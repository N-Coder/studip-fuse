import logging
import os
from asyncio import as_completed
from datetime import datetime
from os import path
from stat import S_IFDIR, S_IFREG, S_IRGRP, S_IROTH, S_IRUSR
from typing import Any, Dict, List, Optional, Set, Tuple, Type, Union

import attr
from cached_property import cached_property
from frozendict import frozendict

from studip_api.downloader import Download
from studip_api.model import Course, File, Semester
from studip_fuse.cache import cached_task
from studip_fuse.path.path_util import Charset, EscapeMode, escape_file_name, get_format_segment_requires, \
    normalize_path, path_head, path_tail

log = logging.getLogger("studip_fuse.virtual_path")


@attr.s(frozen=True, str=False, repr=False, hash=False)
class VirtualPath(object):
    session = attr.ib()  # type: 'StudIPSession'
    path_segments = attr.ib(convert=tuple)  # type: Tuple[str]
    known_data = attr.ib(convert=frozendict)  # type: Union[Dict[Type, Any], frozendict]
    parent = attr.ib()  # type: Optional['VirtualPath']
    next_path_segments = attr.ib(convert=tuple)  # type: Tuple[str]

    # __init__  ########################################################################################################

    @known_data.validator
    def validate(self, *_):
        if not self.is_folder:
            assert self._file, \
                "virtual path %s has no more possible path segments (and thus must be a file, " \
                "not a folder), but doesn't uniquely describe a single file" % self
        if self._course:
            assert not self._semester or self._course.semester == self._semester, \
                "virtual path %s describes course %s of semester %s, " \
                "which doesn't match the semester of the virtual path %s" % \
                (self, self._course, self._course.semester, self._semester)
        if self._file:
            assert not self._semester or self._course.semester == self._semester, \
                "virtual path %s describes file %s of semester %s, " \
                "which doesn't match the semester of the virtual path %s" % \
                (self, self._course, self._course.semester, self._semester)
            assert not self._course or self._file.course == self._course, \
                "virtual path %s describes file %s of course %s %s, " \
                "which doesn't match the course of the virtual path %s %s" % \
                (self, self._file, self._file.course, self._file.course.semester, self._course, self._course.semester)
        inv_keys = set(self.known_data.keys()).difference([File, Course, Semester])
        if inv_keys:
            raise ValueError("invalid keys for known_data: %s" % inv_keys)

    # public properties  ###############################################################################################

    @cached_property
    def partial_path(self):
        path_segments = self.path_segments
        if self._loop_over_path and self._file:
            # preview the file path we're generating in the loop
            path_segments = path_segments + (path_head(self.next_path_segments),)
        partial = "/".join(path_segments).format(**self._known_tokens)
        partial = normalize_path(partial)
        return partial

    @cached_property
    def is_folder(self) -> bool:
        return bool(self.next_path_segments)

    @cached_property
    def is_root(self) -> bool:
        return not self.parent

    # FS-API  ##########################################################################################################

    @cached_task()
    async def list_contents(self) -> List['VirtualPath']:
        assert self.is_folder, "list_contents called on non-folder %s" % self

        if File in self._content_options:
            return await self._list_contents_file_options()

        elif Course in self._content_options:
            if self._course:  # everything is already known, no options on this level
                return [self._sub_path()]
            elif self._semester:
                return [self._sub_path(new_known_data={Course: c})
                        for c in await self.session.get_courses(self._semester)]
            else:
                list = []
                # XXX python 3.5 doesn't support await inside list comprehensions
                # using `as_completed`, first schedule `get_courses` for all `s`, then await the results
                # if `get_courses` would be directly awaited, scheduling and execution would be sequential
                for fcs in as_completed([
                    self.session.get_courses(s)
                    for s in await self.session.get_semesters()
                ]):
                    for course in await fcs:
                        list.append(self._sub_path(new_known_data={Course: course}))
                return list

        elif Semester in self._content_options:
            if self._semester:  # everything is already known, no options on this level
                return [self._sub_path()]
            return [self._sub_path(new_known_data={Semester: s})
                    for s in await self.session.get_semesters()]

        else:
            assert not self._content_options, "unknown content options %s for virtual path %s" % \
                                              (self._content_options, self)
            return [self._sub_path()]

    async def _list_contents_file_options(self) -> List['VirtualPath']:
        assert self.is_folder, "_list_contents_file_options called on non-folder %s" % self

        if self._file:
            if self._loop_over_path and self._file.is_folder():  # loop over contents of one folder #1
                files = [f
                         for f in (await self.session.get_folder_files(self._file)).contents]

            else:  # everything is already known, no options on this level
                return [self._sub_path()]
        else:  # all folders still possible
            if self._course:
                files = [f
                         for f in (await self.session.get_course_files(self._course)).contents]
            elif self._semester:
                files = []
                # XXX python 3.5 doesn't support await inside list comprehensions
                for ffs in as_completed([
                    self.session.get_course_files(c)
                    for c in await self.session.get_courses(self._semester)
                ]):
                    for folder in await ffs:
                        files += folder.contents
            else:
                files = []
                # XXX python 3.5 doesn't support await inside list comprehensions
                for fcs in as_completed([
                    self.session.get_courses(s)
                    for s in await self.session.get_semesters()
                ]):
                    # the following await lead to partially sequential execution in very rare cases
                    for course in await fcs:
                        for ffs in as_completed([
                            self.session.get_course_files(course)
                        ]):
                            for folder in await ffs:
                                files += folder.contents

        return [self._sub_path(new_known_data={File: f}, increment_path_segments=not self._loop_over_path)
                for f in files]

    def access(self, mode):
        pass

    def getattr(self):
        d = dict(st_ino=hash(self.partial_path), st_nlink=1,
                 st_mode=(S_IFDIR if self.is_folder else S_IFREG) | S_IRUSR | S_IRGRP | S_IROTH)
        if hasattr(os, "getuid"):
            d["st_uid"] = os.getuid()
        if hasattr(os, "getgid"):
            d["st_gid"] = os.getgid()
        if self.mod_times[0]:
            d["st_ctime"] = self.mod_times[0].timestamp()
        if self.mod_times[1]:
            d["st_mtime"] = self.mod_times[1].timestamp()
        if not self.is_folder:
            if self._file.size is None:
                log.warning("Size of file %s unknown, because the value wasn't loaded from Stud.IP", self._file)
            else:
                d["st_size"] = self._file.size
        return d

    async def open_file(self, flags) -> Download:
        assert not self.is_folder, "open_file called on folder %s" % self
        return await self.session.download_file_contents(self._file)

    # private properties ###############################################################################################

    def __escape_file(self, str):
        return escape_file_name(str, Charset.Ascii, EscapeMode.Similar)

    def __escape_path(self, folders):
        return path.join(*map(self.__escape_file, folders)) if folders else ""

    @cached_property
    def mod_times(self) -> Tuple[Optional[datetime], Optional[datetime]]:
        if self._file:
            return self._file.created, self._file.changed
        if self._course:
            return (self._course.semester.start_date,) * 2
        if self._semester:
            return (self._semester.start_date,) * 2
        return None, None

    @cached_property
    def _known_tokens(self):
        tokens = {  # TODO time may differ between file and parent folder, which will break path logic
            "created": self.mod_times[0],
            "changed": self.mod_times[1],
        }
        if self._semester:
            tokens.update({
                "semester": self.__escape_file(self._semester.name),
                "semester-lexical": self.__escape_file(self._semester.lexical),
                "semester-lexical-short": self.__escape_file(self._semester.lexical_short),
            })

        if self._course:
            tokens.update({
                "semester": self.__escape_file(self._course.semester.name),
                "semester-lexical": self.__escape_file(self._course.semester.lexical),
                "semester-lexical-short": self.__escape_file(self._course.semester.lexical_short),

                "course-id": self._course.id,
                "course-abbrev": self.__escape_file(self._course.abbrev),
                "course": self.__escape_file(self._course.name),
                "type": self.__escape_file(self._course.type),
                "type-abbrev": self.__escape_file(self._course.type_abbrev),
            })

        if self._file:
            path = short_path = self._file.path[1:-1]  # '1234VL-Name'/[path]/'file_name'

            # skip "Allgemeiner Dateiordner" if it is the only object in course root dir / has no siblings
            root_file = self._file
            while root_file.parent and root_file.parent.parent:
                root_file = root_file.parent
            if root_file.name in ["Allgemeiner Dateiordner", "Hauptordner"] and root_file.is_single_child:
                short_path = path_tail(short_path)

            tokens.update({
                "semester": self.__escape_file(self._file.course.semester.name),
                "semester-lexical": self.__escape_file(self._file.course.semester.lexical),
                "semester-lexical-short": self.__escape_file(self._file.course.semester.lexical_short),

                "course-id": self._file.course.id,
                "course-abbrev": self.__escape_file(self._file.course.abbrev),
                "course": self.__escape_file(self._file.course.name),
                "type": self.__escape_file(self._file.course.type),
                "type-abbrev": self.__escape_file(self._file.course.type_abbrev),

                "path": self.__escape_path(path),
                "short-path": self.__escape_path(short_path),

                "id": self._file.id,
                "name": self.__escape_file(self._file.name),
                "description": self.__escape_file(self._file.description or ""),
                "author": self.__escape_file(self._file.author or ""),
            })
        return tokens

    @cached_property
    def _file(self) -> File:
        return self.known_data.get(File, None)

    @cached_property
    def _course(self) -> Course:
        return self.known_data.get(Course, None)

    @cached_property
    def _semester(self) -> Semester:
        return self.known_data.get(Semester, None)

    @cached_property
    def _loop_over_path(self):
        return self.is_folder and any(t in path_head(self.next_path_segments) for t in ["{path}", "{short-path}"])

    @cached_property
    def _content_options(self) -> Set[Type]:
        if self.is_folder:
            return get_format_segment_requires(path_head(self.next_path_segments))
        else:
            return set()

    def _sub_path(self, new_known_data=None, increment_path_segments=True, **kwargs):
        assert self.is_folder, "_sub_path called on non-folder %s" % self
        args = dict(session=self.session, parent=self, known_data=self.known_data)
        if increment_path_segments:
            args.update(path_segments=self.path_segments + (path_head(self.next_path_segments),),
                        next_path_segments=path_tail(self.next_path_segments))
        else:
            args.update(path_segments=self.path_segments,
                        next_path_segments=self.next_path_segments)
        if new_known_data:
            args["known_data"] = dict(args["known_data"])
            args["known_data"].update(new_known_data)
        args.update(kwargs)
        for type, value in args["known_data"].items():
            assert isinstance(value, type), "known_data item '%s' must be of type %s" % (value, type)
        return VirtualPath(**args)

    # utils  ###########################################################################################################

    def __hash__(self):
        return hash((self.path_segments, self.known_data, self.parent, self.next_path_segments))

    def __str__(self):
        path_segments = [seg.format(**self._known_tokens) for seg in self.path_segments]

        if self._loop_over_path and self._file:
            # preview the file path we're generating in the loop
            preview_file_path = path_head(self.next_path_segments).format(**self._known_tokens)
            if preview_file_path:
                path_segments.append("(" + preview_file_path + ")")

        path_segments += self.next_path_segments

        options = "[%s]->[%s]" % (
            ",".join(c.__name__ for c in self.known_data.keys()),
            ",".join(c.__name__ for c in self._content_options))
        return "[%s](%s)" % (
            "/".join(filter(bool, path_segments)),
            ",".join(filter(bool, [
                "root" if self.is_root else None,
                "folder" if self.is_folder else "file",
                "loop_path" if self._loop_over_path else None,
                options
            ]))
        )

    def __repr__(self):
        return "VirtualPath(%s)" % str(self)

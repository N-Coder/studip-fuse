import asyncio
import itertools
import logging
import os
from asyncio import Queue
from datetime import datetime
from enum import IntEnum
from os import path
from stat import S_IFDIR, S_IFREG, S_IRGRP, S_IROTH, S_IRUSR
from typing import Any, Dict, Optional, Set, Tuple

import attr
from async_generator import async_generator, yield_
from cached_property import cached_property

from studip_fuse.avfs.path_util import path_head, path_tail
from studip_fuse.avfs.virtual_path import FormatToken, VirtualPath, get_format_str_fields
from studip_fuse.studipfs.api.downloader import Download
from studip_fuse.studipfs.api.session import StudIPSession
from studip_fuse.studipfs.path.encoding import Charset, EscapeMode, escape_file_name

log = logging.getLogger(__name__)


# TODO move
class Pipeline(object):
    done_obj = object()

    def __init__(self):
        self.queues = [Queue()]
        self.tasks = []

    def put(self, item):
        self.queues[0].put_nowait(item)

    @async_generator
    async def drain(self):
        self.queues[0].put_nowait(self.done_obj)
        await asyncio.gather(*self.tasks)

        queue = self.queues[-1]
        while True:
            item = await queue.get()
            try:
                if item is self.done_obj:
                    break
                else:
                    await yield_(item)
            finally:
                queue.task_done()

    async def __processor(self, in_queue, out_queue, func):
        while True:
            item = await in_queue.get()
            try:
                if item is self.done_obj:
                    out_queue.put_nowait(self.done_obj)
                    break
                else:
                    await func(item, out_queue)
            finally:
                in_queue.task_done()

    def add_processor(self, func):
        in_queue = self.queues[-1]
        out_queue = Queue()
        self.queues.append(out_queue)
        self.tasks.append(self.__processor(in_queue=in_queue, out_queue=out_queue, func=func))


class DataField(IntEnum):
    Semester = 1
    Course = 2
    File = 3


File = DataField.File
Course = DataField.Course
Semester = DataField.Semester


@attr.s(frozen=True, str=False, repr=False, hash=False)
class StudIPPath(VirtualPath):
    session = attr.ib()  # type: StudIPSession

    def validate(self):
        inv_keys = set(self.known_data.keys()).difference(DataField)
        if inv_keys:
            raise ValueError("Invalid keys for known_data: %s" % inv_keys)

        if not self.is_folder:
            assert self._file, \
                "Virtual path %s has no more possible path segments (and thus must be a file, " \
                "not a folder), but doesn't uniquely describe a single file. " \
                "Does your path format specification make sense?" % self

        super().validate()

    # FS-API  ##########################################################################################################

    @async_generator
    async def list_contents(self):
        assert self.is_folder, "list_contents called on non-folder %s" % self

        has = max(self.known_data.keys(), default=0)
        wants = max(self.content_options, default=0)

        if has == wants == DataField.File and self.segment_needs_expand_loop and self._file.is_folder:
            # loop over contents of one folder
            folder, subfolders, files = self.session.get_folder_details(self._file)
            for data in itertools.chain(subfolders, files):
                await yield_(self._mk_sub_path(new_known_data=data, increment_path_segments=False))
        elif wants <= has:
            # we already know all we want to know
            await yield_(self._mk_sub_path())
        else:
            needs = [field for field in DataField if has < field <= wants]
            pipeline = Pipeline()
            if Semester in needs:
                pipeline.add_processor(self.__list_semesters)
            if Course in needs:
                pipeline.add_processor(self.__list_courses)
            if File in needs:
                pipeline.add_processor(self.__list_root_file)
            pipeline.put(self.known_data)
            async for data in pipeline.drain():
                await yield_(self._mk_sub_path(new_known_data=data))

    async def __list_semesters(self, item, out_queue):
        async for semester in self.session.get_semesters():
            out_queue.put_nowait({Semester: semester})

    async def __list_courses(self, item, out_queue):
        semester = item[Semester]
        async for course in self.session.get_courses(semester):
            out_queue.put_nowait({Semester: semester, Course: course})

    async def __list_root_file(self, item, out_queue):
        semester = item[Semester]
        course = item[Course]
        folder, subfolders, files = await self.session.get_course_root_file(course)
        out_queue.put_nowait({Semester: semester, Course: course, File: folder})

    def getattr(self):
        d = dict(st_ino=hash(self.partial_path), st_nlink=1,
                 st_mode=S_IFDIR if self.is_folder else S_IFREG)
        if self.is_folder or self._file.is_accessible:
            d["st_mode"] |= S_IRUSR | S_IRGRP | S_IROTH
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

    # public properties  ###############################################################################################

    @cached_property
    def content_options(self) -> Set[DataField]:
        format_segment = path_head(self.next_path_segments)
        requirements = set()
        for field_name in get_format_str_fields(format_segment):
            if field_name in ["semester", "semester-lexical", "semester-lexical-short"]:
                requirements.add(Semester)
            elif field_name in ["course", "course-abbrev", "course-id", "type", "type-abbrev"]:
                requirements.add(Course)
            elif field_name in ["path", "short-path", "id", "name", "description", "author"]:
                requirements.add(File)
            elif field_name == "time" and not requirements:  # any info can provide a time
                requirements.add(Semester)
            else:
                raise ValueError("Unknown format field name '%s' in format string '%s'" % (field_name, format_segment))
        return requirements

    @cached_property
    def segment_needs_expand_loop(self) -> bool:
        return self.is_folder and any(t in path_head(self.next_path_segments) for t in ["{path}", "{short-path}"])

    @cached_property
    def known_tokens(self) -> Dict[FormatToken, Any]:
        tokens = {  # TODO time may differ between file and parent folder, which will break path logic (see issue #2)
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

    # utils  ###########################################################################################################

    def __escape_file(self, str):
        return escape_file_name(str, Charset.Ascii, EscapeMode.Similar)

    def __escape_path(self, folders):
        return path.join(*map(self.__escape_file, folders)) if folders else ""

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
    def mod_times(self) -> Tuple[Optional[datetime], Optional[datetime]]:
        if self._file:
            return self._file.created, self._file.changed
        if self._course:
            return (self._course.semester.start_date,) * 2
        if self._semester:
            return (self._semester.start_date,) * 2
        return None, None

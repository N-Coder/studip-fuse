import errno
import logging
import os
import re
import warnings
from datetime import datetime
from enum import IntEnum
from stat import S_IFDIR, S_IFREG, S_IRGRP, S_IROTH, S_IRUSR
from typing import Any, Dict, Optional, Set, Tuple, Type

import attr
from async_generator import async_generator, yield_
from cached_property import cached_property

from studip_fuse.avfs.path_util import join_path, path_head, path_name
from studip_fuse.avfs.virtual_path import FormatToken, VirtualPath, get_format_str_fields
from studip_fuse.launcher.fuse import FuseOSError
from studip_fuse.studipfs.api.aiointerface import Download, Pipeline
from studip_fuse.studipfs.api.session import StudIPSession
from studip_fuse.studipfs.path.encoding import Charset, EscapeMode, escape_file_name

log = logging.getLogger(__name__)


class DataField(IntEnum):
    Semester = 1
    Course = 2
    Folder = 3
    File = 4


Semester = DataField.Semester
Course = DataField.Course
Folder = DataField.Folder
File = DataField.File


class Abbrev:
    SEMESTER_RE = re.compile(r'^(SS|WS) (\d{2})(.(\d{2}))?')
    WORD_SEPARATOR_RE = re.compile(r'[-. _/()]+')
    NUMBER_RE = re.compile(r'^([0-9]+)|([IVXLCDM]+)$')

    @classmethod
    def semester_lexical_short(cls, title):
        return cls.SEMESTER_RE.sub(r'20\2\1', title)

    @classmethod
    def semester_lexical(cls, title):
        return cls.SEMESTER_RE.sub(r'20\2 \1 -\4', title).rstrip(" -")

    @classmethod
    def course_abbrev(cls, name):
        words = cls.WORD_SEPARATOR_RE.split(name)
        number = ""
        abbrev = ""
        if len(words) > 1 and cls.NUMBER_RE.match(words[-1]):
            number = words[-1]
            words = words[0:len(words) - 1]
        if len(words) < 3:
            abbrev = "".join(w[0: min(3, len(w))] for w in words)
        elif len(words) >= 3:
            abbrev = "".join(w[0] for w in words if len(w) > 0)
        return abbrev + number

    @classmethod
    def course_type_abbrev(cls, typ):
        # TODO use data from complete type listing
        special_abbrevs = {
            "Arbeitsgemeinschaft": "AG",
            "Studien-/Arbeitsgruppe": "SG",
        }
        try:
            return special_abbrevs[typ]
        except KeyError:
            abbrev = typ[0]
            if typ.endswith("seminar"):
                abbrev += "S"
            return abbrev


@attr.s(frozen=True, str=False, repr=False, hash=False)
class StudIPPath(VirtualPath):
    session = attr.ib()  # type: StudIPSession
    pipeline_type = attr.ib()  # type: Type[Pipeline]

    def validate(self):
        inv_keys = set(self.known_data.keys()).difference(DataField)
        if inv_keys:
            raise ValueError("Invalid keys for known_data: %s" % inv_keys)

        if not self.is_folder:
            assert self._folder and self._folder.get("folder_type", None) and \
                   self._file and self._file.get("mime_type", None), \
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

        if wants <= has and not self.segment_needs_expand_loop:
            # we already know all we want to know
            await yield_(self._mk_sub_path())
        else:
            needs = [field for field in DataField if has < field <= wants]
            pipeline = self.pipeline_type()
            if Semester in needs:
                pipeline.add_processor(self.__list_semesters)
            if Course in needs:
                pipeline.add_processor(self.__list_courses)
            if Folder in needs:
                pipeline.add_processor(self.__get_root_folder)
            if File in needs or self.segment_needs_expand_loop:
                pipeline.add_processor(self.__list_folder_contents)
                pipeline.add_processor(self.__inflate_file_ids)
            pipeline.put(self.known_data)
            async for data in pipeline.drain():
                if not isinstance(data, VirtualPath):
                    data = self._mk_sub_path(new_known_data=data)
                await yield_(data)

    async def __list_semesters(self, item, out_queue):
        async for semester in self.session.get_semesters():
            out_queue.put_nowait({Semester: semester})

    async def __list_courses(self, item, out_queue):
        semester = item[Semester]
        async for course in self.session.get_courses(semester):
            out_queue.put_nowait({Semester: semester, Course: course})

    async def __get_root_folder(self, item, out_queue):
        semester = item[Semester]
        course = item[Course]
        folder, subfolders, files = await self.session.get_course_root_folder(course)
        out_queue.put_nowait({Semester: semester, Course: course, Folder: folder})

    async def __list_folder_contents(self, item, out_queue):
        folder, subfolders, files = await self.session.get_folder_details(item[Folder])
        for subfolderid in subfolders:
            out_queue.put_nowait({**item, Folder: subfolderid})
        for fileid in files:
            out_queue.put_nowait({**item, File: fileid})

    async def __inflate_file_ids(self, item, out_queue):
        if File in item and isinstance(item[File], str):
            file = await self.session.get_file_details(item[File])
            out_queue.put_nowait({**item, File: file})
        elif Folder in item and isinstance(item[Folder], str):
            subfolder, subsubfolders, subfiles = await self.session.get_folder_details(item[Folder])
            if not subfolder.get("is_visible", False) or not subfolder.get("is_readable", False):
                warnings.warn("Ignoring non-readable folder with id %s: %s" % (item[Folder], subfolder))
            else:
                # we need to set increment_path_segments=False, so put full VirtualPath object instead of data dict into queue
                out_queue.put_nowait(self._mk_sub_path(
                    new_known_data={**item, Folder: subfolder},
                    increment_path_segments=False))

    async def access(self, mode):
        if self._file and not (self._file.get("is_downloadable", True) and self._file.get("is_readable", True)):
            raise FuseOSError(errno.EACCES)
        await super(StudIPPath, self).access(mode)

    async def getattr(self):
        d = dict(st_ino=hash(self.partial_path), st_nlink=1,
                 st_mode=S_IFDIR if self.is_folder else S_IFREG)
        if self.is_folder or (self._file.get("is_downloadable", True) and self._file.get("is_readable", True)):
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
            d["st_size"] = int(self._file["size"])
        return d

    async def getxattr(self):
        xattrs = dict(self.known_tokens)
        # TODO missing all folder data
        #   "author-id": self.__escape(self._folder["user_id"]),  # 'cli'
        #   "is_visible": self.__escape(self._folder["is_visible"]),  # True
        #   "is_readable": self.__escape(self._folder["is_readable"]),  # True
        #   "is_writable": self.__escape(self._folder["is_writable"]),  # True
        # TODO file doesn't carry information on accessibility
        # TODO "user.studip-fuse.contents-status" / "user.studip-fuse.contents-exception" for folder / file get_content_task
        import json
        from pyrsistent import thaw
        xattrs["json"] = json.dumps({
            "semester": thaw(self._semester),
            "course": thaw(self._course),
            "folder": thaw(self._folder),
            "file": thaw(self._file)
        })
        return xattrs

    async def open_file(self, flags) -> Download:
        await self.access(flags)
        assert not self.is_folder, "open_file called on folder %s" % self
        return await self.session.retrieve_file(self._file)

    # public properties  ###############################################################################################

    @cached_property
    def content_options(self) -> Set[DataField]:
        requirements = set()
        if self.next_path_segments:
            format_segment = path_head(self.next_path_segments)
            for field_name in get_format_str_fields(format_segment):
                # TODO course_class, course_type, folder_type and file_terms are well defined and can be listed without listing all courses/folders/files
                # TODO update available field_names
                if field_name in ["semester-id", "semester", "semester-short", "semester-lexical", "semester-lexical-short"]:
                    requirements.add(Semester)
                elif field_name in ["course-id", "course-number", "course", "course-subtitle", "course-description",
                                    "course-abbrev", "course-type", "course-class", "course-type-abbrev", "course-type-short",
                                    "course-location", "course-grouping"]:
                    requirements.add(Course)
                elif field_name in ["path", "short-path"]:
                    requirements.add(Folder)
                elif field_name in ["file-id", "file-name", "file-description", "file-size", "file-mime-type",
                                    "file-terms", "file-storage", "file-downloads"]:
                    requirements.add(File)
                else:
                    raise ValueError("Unknown format field name '%s' in format string '%s'" % (field_name, format_segment))
        return requirements

    @cached_property
    def segment_needs_expand_loop(self) -> bool:
        return self.path_segments and any(t in path_name(self.path_segments) for t in ["{path}", "{short-path}"])

    @cached_property
    def known_tokens(self) -> Dict[FormatToken, Any]:
        path, short_path = self.known_folder_path
        tokens = {
            "path": self.__escape_path(path),
            "short-path": self.__escape_path(short_path),
        }
        if self._semester:
            tokens.update({
                "semester-id": self._semester["id"],  # '4cb8438b3057e71a627ab7e25d73ba75'
                "semester": self.__escape(self._semester["description"]),  # 'Wintersemester 2017/2018'
                "semester-short": self.__escape(self._semester["title"]),  # 'WS 17/18'
                "semester-lexical": self.__escape(Abbrev.semester_lexical(self._semester["title"])),
                "semester-lexical-short": self.__escape(Abbrev.semester_lexical_short(self._semester["title"])),

                # 'seminars_end': 1518303599,
                # 'begin': 1506808800,
                # 'end': 1522533599,
                # 'seminars_begin': 1508104800
            })
        if self._course:
            number = re.sub("[^0-9]", "", str(self._course["number"]))
            type_abbrev = re.sub("[0-9]", "", str(self._course["number"]))
            type_obj = self.session.studip_course_type[self._course["type"]]
            # map for [int(id), str(id) and name] -> {'id': 21, 'name': 'Workshop', 'class': '3'}
            clazz_obj = self.session.studip_course_class[type_obj["class"]]
            # map for [int(id), str(id) and name] -> {'id': 4, 'name': 'Studien-/Arbeitsgruppen', ...}
            tokens.update({
                "course-id": self._course["course_id"],  # '00093e6878c6c7733579251567a177da'
                "course-number": self.__escape(number),  # '5795'
                "course": self.__escape(self._course["title"]),  # 'Virtuelle Maschinen und Laufzeitsysteme'
                "course-subtitle": self.__escape(self._course["subtitle"]),  # ''
                "course-description": self.__escape(self._course["description"]),  # ''
                "course-abbrev": self.__escape(Abbrev.course_abbrev(self._course["title"])),
                "course-type-abbrev": self.__escape(type_abbrev),
                "course-type": self.__escape(type_obj["name"]),  # 'Uebung'
                "course-type-short": self.__escape(Abbrev.course_type_abbrev(type_obj["name"])),  # 'U'
                "course-class": self.__escape(clazz_obj["name"]),  # 'Lehre'
                "course-location": self.__escape(self._course["location"]),  # ''
                "course-group": self.__escape(self._course["group"]),  # 1

                # 'start_semester': '/studip/api.php/semester/4cb8438b3057e71a627ab7e25d73ba75',
                # 'end_semester': '/studip/api.php/semester/4cb8438b3057e71a627ab7e25d73ba75'
            })
        if self._file:
            tokens.update({
                "file-id": self.__escape(self._file["id"]),  # '3c90ca04794bce6661f985c664a5d6cd',
                "file-author": self.__escape(self._file["user_id"]),  # 'cli'
                "file-name": self.__escape(self._file["name"]),  # 'Virtuelle Maschinen und Laufzeitsysteme'
                "file-description": self.__escape(self._file["description"]),  # ''
                "file-size": self.__escape(self._file["size"]),  # '118738',
                "file-mime-type": self.__escape(self._file["mime_type"]),  # 'application/pdf',
                "file-terms": self.__escape(self._file["content_terms_of_use_id"]),  # 'SELFMADE_NONPUB',
                "file-storage": self.__escape(self._file["storage"]),  # 'disk',
                "file-downloads": self.__escape(self._file["downloads"]),  # '118',
            })
        return tokens

    @cached_property
    def known_folder_path(self) -> Tuple[str, str]:
        path = short_path = []
        vp = self
        visited_folders = []
        while vp and vp._folder:
            if vp._folder not in visited_folders:
                if visited_folders:
                    # we are walking the hierarchy up, so the next upper folder should be the parent of the lower folder
                    assert vp._folder["id"] == visited_folders[-1]["parent_id"]
                visited_folders.append(vp._folder)
                path = [vp._folder["name"]] + path

                is_ghost_folder = vp._folder.get("folder_type", None) in ["RootFolder"]
                guess_ghost_folder = vp._folder["name"] in ["Allgemeiner Dateiordner", "Hauptordner", self._course["title"] if self._course else None]
                if is_ghost_folder != guess_ghost_folder and vp._folder.get("folder_type", None) not in ["StandardFolder", "PermissionEnabledFolder"]:
                    warnings.warn("Folder has name %s indicating a ghost folder, but type is %s: %s" % (
                        vp._folder["name"], vp._folder["folder_type"], vp._folder))
                if not (is_ghost_folder or guess_ghost_folder):
                    short_path = [vp._folder["name"]] + short_path

            assert vp is not vp.parent
            vp = vp.parent

        return path, short_path

    # utils  ###########################################################################################################

    def __escape(self, str):
        return escape_file_name(str, Charset.Ascii, EscapeMode.Similar)

    def __escape_path(self, folders):
        return join_path(*map(self.__escape, folders)) if folders else ""

    @cached_property
    def _file(self):
        return self.known_data.get(File, None)

    @cached_property
    def _folder(self):
        return self.known_data.get(Folder, None)

    @cached_property
    def _course(self):
        return self.known_data.get(Course, None)

    @cached_property
    def _semester(self):
        return self.known_data.get(Semester, None)

    @cached_property
    def mod_times(self) -> Tuple[Optional[datetime], Optional[datetime]]:
        vals = None
        if self._semester:
            # "begin", "end", "seminars_begin", "seminars_end"
            vals = (self._semester["begin"],) * 2
        if self._course:
            vals = (self._course["start_date"],) * 2
        if self._folder:
            vals = self._folder["mkdate"], self._folder["chdate"]
        if self._file:
            vals = self._file["mkdate"], self._file["chdate"]

        if vals:
            # noinspection PyTypeChecker
            return tuple(datetime.fromtimestamp(int(val)) for val in vals)
        else:
            return None, None

    def known_data_str(self, key: DataField, value):
        if key == DataField.Course:
            return "%s(%s %s %s)" % (key, value["number"], value["type"], value["title"])
        else:
            return "%s(%s)" % (key, getattr(value, "title", getattr(value, "name", getattr(value, "id", "?"))))

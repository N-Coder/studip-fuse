import errno
import logging
import os
import re
import warnings
from datetime import datetime
from enum import IntEnum
from stat import S_IFDIR, S_IFREG, S_IRGRP, S_IROTH, S_IRUSR
from typing import Optional, Tuple, Type

import attr
from async_generator import async_generator, yield_
from cached_property import cached_property
from refuse.high import FuseOSError

from studip_fuse.avfs.path_util import join_path, path_name
from studip_fuse.avfs.virtual_path import FormatTokenGeneratorVirtualPath, VirtualPath
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
    SEMESTER_RE = re.compile(r'^(So?Se?|Wi?Se?) (\d{2})(.(\d{2}))?')
    WORD_SEPARATOR_RE = re.compile(r'[-. _/()]+')
    NUMBER_RE = re.compile(r'^([0-9]+)|([IVXLCDM]+)$')

    @classmethod
    def semester_lexical_short(cls, title):
        return re.sub("[a-z]", "", cls.SEMESTER_RE.sub(r'20\2\1', title))

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


@attr.s(frozen=True, str=False, repr=False, hash=False)
class StudIPPath(FormatTokenGeneratorVirtualPath):
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
        import json
        from pyrsistent import thaw

        xattrs = {"known-tokens": json.dumps(self.known_tokens)}

        if self.is_folder:
            # list_contents is not cached, so we don't know here whether that information is available
            # see studip_fuse.launcher.aioimpl.asyncio.alru_realpath.CachingRealPath for an implementation
            pass
        else:
            try:
                download = await self.open_file()
                if download.is_loading:
                    xattrs["contents-status"] = "pending"
                    xattrs["contents-exception"] = "InvalidStateError: operation is not complete yet"
                elif download.is_completed:
                    xattrs["contents-status"] = "available"
                    xattrs["contents-exception"] = ""
                elif download.exception():
                    xattrs["contents-status"] = "failed"
                    xattrs["contents-exception"] = download.exception()
                else:
                    xattrs["contents-status"] = "unknown"
                    xattrs["contents-exception"] = "InvalidStateError: operation was not started yet"
            except FuseOSError as e:
                xattrs["contents-status"] = "unavailable"
                xattrs["contents-exception"] = e
        if isinstance(xattrs.get("contents-exception", None), BaseException):
            exc = xattrs["contents-exception"]
            xattrs["contents-exception"] = "%s: %s" % (type(exc).__name__, exc)

        url = "/studip/dispatch.php/"
        if self._file:
            url += "file/details/%s?cid=%s" % (self._file["id"], self._course["course_id"])
        elif self._folder:
            url += "course/files/index/%s?cid=%s" % (self._folder["id"], self._course["course_id"])
        elif self._course:
            url += "course/files?cid=%s" % (self._course["course_id"])
        elif self._semester:
            url += "my_courses/set_semester?sem_select=%s" % (self._semester["id"])
        else:
            url += "my_courses"
        xattrs["url"] = self.session.studip_url(url)

        xattrs["json"] = json.dumps({
            "semester": thaw(self._semester),
            "course": thaw(self._course),
            "folder": thaw(self._folder),
            "file": thaw(self._file)
        })
        return xattrs

    async def open_file(self, flags=None) -> Download:
        await self.access(flags)
        assert not self.is_folder, "open_file called on folder %s" % self
        return await self.session.retrieve_file(self._file)

    # public properties  ###############################################################################################

    @classmethod
    def get_format_token_generators(cls):
        from studip_fuse.avfs.virtual_path import FormatToken, FormatTokenGenerator as FTG

        class SimpleFTG(FTG):
            def __init__(self, key: FormatToken, req_key, val_key, doc=None):
                super().__init__(key, [req_key], self.generator, doc)
                self.req_key = req_key
                self.val_key = val_key

            def generator(self, vp: "StudIPPath"):
                return vp.escape(vp.known_data[self.req_key][self.val_key])

        return [
            FTG("path", [],
                lambda self: self.escape_path(self.known_folder_path[0]),
                """path to the file, relative to the root folder of the course"""),
            FTG("short-path", [],
                lambda self: self.escape_path(self.known_folder_path[1]),
                """path to the file, relative to the root folder of the course, stripped from common parts"""),

            FTG("semester-lexical", [Semester],
                lambda self: self.escape(Abbrev.semester_lexical(self._semester["title"])),
                """full semester name, allowing alphabetic sorting"""),
            FTG("semester-lexical-short", [Semester],
                lambda self: self.escape(Abbrev.semester_lexical_short(self._semester["title"])),
                """shortened semester name, allowing alphabetic sorting"""),

            FTG("course-number", [Course],
                lambda self: self.escape(re.sub("[^0-9]", "", str(self._course["number"]))),
                """number assigned to the course in the course catalogue"""),
            FTG("course-abbrev", [Course],
                lambda self: self.escape(Abbrev.course_abbrev(self._course["title"])),
                """abbreviation of the course name, generated from its initials"""),
            FTG("course-type-short", [Course],
                lambda self: self.escape(re.sub("[0-9]", "", str(self._course["number"]))),
                """abbreviated type of the course, usually the letter appended to the course number in the course catalogue"""),

            SimpleFTG("semester-id", Semester, "id",
                      """system-internal hexadecimal UUID of the semester"""),
            SimpleFTG("semester", Semester, "description",
                      """full semester name"""),
            SimpleFTG("semester-short", Semester, "title",
                      """shortened semester name"""),

            SimpleFTG("course-id", Course, "course_id",
                      """system-internal hexadecimal UUID of the course"""),
            SimpleFTG("course", Course, "title",
                      """official name of the course, usually excluding its type"""),
            SimpleFTG("course-subtitle", Course, "subtitle",
                      """optional subtitle assigned to the course"""),
            SimpleFTG("course-description", Course, "description",
                      """optional description given for the course"""),
            SimpleFTG("course-type", Course, "type",
                      """type of the course (lecture, exercise,...)"""),
            SimpleFTG("course-class", Course, "class",
                      """type of the course (teaching, community,...)"""),
            SimpleFTG("course-location", Course, "location",
                      """room where the course is held"""),
            SimpleFTG("course-group", Course, "group",
                      """user-assigned (color-)group of the course on the Stud.IP overview page"""),

            SimpleFTG("file-id", File, "id",
                      """system-internal hexadecimal UUID of the file"""),
            # SimpleFTG("file-author", File, "user_id", # unfortunately, we would need another API call to resolve the name
            #           """the UUID of the person that uploaded this file"""),
            SimpleFTG("file-name", File, "name",
                      """(base-)name of the file, including its extension"""),
            SimpleFTG("file-description", File, "description",
                      """optional description given for the file"""),
            SimpleFTG("file-size", File, "size",
                      """file size in bytes"""),
            SimpleFTG("file-mime-type", File, "mime_type",
                      """file's mime-type detected by Stud.IP"""),
            SimpleFTG("file-terms", File, "content_terms_of_use_id",
                      """terms on which the file might be used"""),
            SimpleFTG("file-storage", File, "storage",
                      """how the file is stored on the Stud.IP server"""),
            SimpleFTG("file-downloads", File, "downloads",
                      """number of times the file has been downloaded"""),
        ]

    @cached_property
    def segment_needs_expand_loop(self) -> bool:
        return self.path_segments and any(t in path_name(self.path_segments) for t in ["{path}", "{short-path}"])

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
                guess_ghost_folder = vp._folder["name"] in [
                    None, "", "Allgemeiner Dateiordner", "Hauptordner",
                    self._course["title"] if self._course else None]
                is_special_folder = vp._folder.get("folder_type", None) not in ["StandardFolder", "PermissionEnabledFolder"]
                if guess_ghost_folder and not is_ghost_folder and is_special_folder:
                    warnings.warn("Folder has name '%s' indicating a ghost folder, but type is %s: %s" % (
                        vp._folder["name"], vp._folder["folder_type"], vp._folder))
                if not (is_ghost_folder or guess_ghost_folder):
                    short_path = [vp._folder["name"]] + short_path

            assert vp is not vp.parent
            vp = vp.parent

        return path, short_path

    # utils  ###########################################################################################################

    def escape(self, val):
        # TODO make customizable?
        return escape_file_name(val, Charset.Ascii, EscapeMode.Similar)

    def escape_path(self, folders):
        return join_path(*map(self.escape, folders)) if folders else ""

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

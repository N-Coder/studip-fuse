import json
import os
import time
from itertools import groupby
from stat import S_ISREG

import aiofiles.os as aio_os
import attr

from studip_api.downloader import Download
from studip_api.model import *
from studip_api.session import StudIPSession, log
from studip_fuse.cache import cached_download, cached_task


@attr.s(hash=False)
class CachedStudIPSession(StudIPSession):
    cache_dir = attr.ib()  # type: str

    def __attrs_post_init__(self):
        super().__attrs_post_init__()
        self.parser.SemesterFactory = Semester.get_or_create
        self.parser.CourseFactory = Course.get_or_create
        self.parser.FileFactory = File.get_or_create
        self.parser.FolderFactory = Folder.get_or_create

    def save_model(self):
        start = time.perf_counter()
        with open(os.path.join(self.cache_dir, "model_data.json"), "wt") as f:
            json.dump(ModelObjectMeta.export_all_data(), f)
            return "stored, took %ss" % (time.perf_counter() - start)

    def load_model(self, update=False):
        start = time.perf_counter()
        with open(os.path.join(self.cache_dir, "model_data.json"), "rt") as f:
            ModelObjectMeta.import_all_data(json.load(f), update)

        def set_fb(method, value, *key_args, **key_kwargs):
            method._set_fallback_value(method._make_key((self,) + key_args, key_kwargs), value, overwrite=False)

        set_fb(self.get_semesters, Semester.INSTANCES, {})

        for semester, courses in groupby(sorted(Course.INSTANCES, key=lambda c: c.semester.id), key=Course.semester):
            set_fb(self.get_courses, list(courses), semester)

        for file in File.INSTANCES:
            if file.is_root:
                set_fb(self.get_course_files, file, file.course)
            elif file.is_folder():
                set_fb(self.get_folder_files, file, file)

        return "loaded, took %ss" % (time.perf_counter() - start)

    @cached_task()
    async def get_semesters(self):
        return await super().get_semesters()

    @cached_task()
    async def get_courses(self, semester):
        return await super().get_courses(semester)

    @cached_task()
    async def get_course_files(self, course):
        return await super().get_course_files(course)

    @cached_task()
    async def get_folder_files(self, folder):
        return await super().get_folder_files(folder)

    @cached_download()
    async def download_file_contents(self, studip_file, local_dest=None, chunk_size=1024 * 256):
        if not local_dest:
            local_dest = os.path.join(self.cache_dir, studip_file.id)

        # check integrity of existing paths (file with id exists, same size, same change date) and reuse them
        timestamp = time.mktime(studip_file.changed.timetuple())
        try:
            stat = await aio_os.stat(local_dest)
            if S_ISREG(stat.st_mode) and stat.st_size == studip_file.size and stat.st_mtime == timestamp:
                log.info("Re-using existing file for download %s -> %s", studip_file, local_dest)
                download = Download(self.ahttp, self._get_download_url(studip_file), local_dest, chunk_size)
                await download.load_completed()
                return download
        except FileNotFoundError:
            pass

        return await super().download_file_contents(studip_file, local_dest)

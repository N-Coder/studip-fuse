import json
import os
import time
from stat import S_ISREG

import aiofiles.os as aio_os
import attr

from studip_api.downloader import Download
from studip_api.model import *
from studip_api.session import StudIPSession, log
from studip_fuse.cache import DownloadTaskCache, ModelGetterCache, cached_task
from studip_fuse.cache.circuit_breaker import NetworkCircuitBreaker


@attr.s(hash=False)
class CachedStudIPSession(StudIPSession):
    cache_dir = attr.ib()  # type: str

    def __attrs_post_init__(self):
        self.circuit_breaker = NetworkCircuitBreaker()
        self._http_args["trace_configs"] = [self.circuit_breaker.trace_config]

        super().__attrs_post_init__()

        self.parser.SemesterFactory = Semester.get_or_create
        self.parser.CourseFactory = Course.get_or_create
        self.parser.FileFactory = File.get_or_create
        self.parser.FolderFactory = Folder.get_or_create

        for func in (self.get_semesters, self.get_courses, self.get_course_files, self.get_folder_files):
            func._may_create = self.circuit_breaker.may_create
            func._exception_handler = self.circuit_breaker.exception_handler

    def save_model(self):
        start = time.perf_counter()
        with open(os.path.join(self.cache_dir, "model_data.json"), "wt") as f:
            data = ModelObjectMeta.export_all_data()
            for func in (self.get_semesters, self.get_courses, self.get_course_files, self.get_folder_files):
                data[func.__name__] = func.export_cache()
            json.dump(data, f)
            return "stored, took %ss" % (time.perf_counter() - start)

    def load_model(self, update=False):
        start = time.perf_counter()
        with open(os.path.join(self.cache_dir, "model_data.json"), "rt") as f:
            data = json.load(f)
            func_imports = [(func, data.pop(func.__name__)) for func in
                            (self.get_semesters, self.get_courses, self.get_course_files, self.get_folder_files)]

            ModelObjectMeta.import_all_data(data, update)

            for f, d in func_imports:
                f.import_cache(d, update, create_future=self._loop.create_future)
        return "loaded, took %ss" % (time.perf_counter() - start)

    def model_cache_stats(self):
        return {
            "known_semesters": len(Semester.INSTANCES),
            "known_courses": len(Course.INSTANCES),
            "known_files": len(File.INSTANCES),
            "indexed_semesters": len(self.get_courses._cache),
            "indexed_courses": len(self.get_course_files._cache),
            "indexed_folders": len(self.get_folder_files._cache)
        }

    @cached_task(cache_class=ModelGetterCache)
    async def get_semesters(self):
        return await super().get_semesters()

    @cached_task(cache_class=ModelGetterCache)
    async def get_courses(self, semester):
        return await super().get_courses(semester)

    @cached_task(cache_class=ModelGetterCache)
    async def get_course_files(self, course):
        return await super().get_course_files(course)

    @cached_task(cache_class=ModelGetterCache)
    async def get_folder_files(self, folder):
        return await super().get_folder_files(folder)

    @cached_task(cache_class=DownloadTaskCache)
    async def download_file_contents(self, studip_file, local_dest=None, chunk_size=1024 * 256):
        # FIXME add explicit calls to circuit breaker to individual download parts, potentially allowing retrying failed parts
        if not local_dest:
            local_dest = os.path.join(self.cache_dir, studip_file.id)

        if await self.has_cached_download(studip_file, local_dest):
            log.info("Re-using existing file for download %s -> %s", studip_file, local_dest)
            download = Download(self.ahttp, self._get_download_url(studip_file), local_dest, chunk_size)
            await download.load_completed()
            return download

        return await super().download_file_contents(studip_file, local_dest)

    async def has_cached_download(self, studip_file, local_dest=None):
        if not local_dest:
            local_dest = os.path.join(self.cache_dir, studip_file.id)

        # check integrity of existing paths (file with id exists, same size, same change date) and reuse them
        timestamp = time.mktime(studip_file.changed.timetuple())
        try:
            stat = await aio_os.stat(local_dest)
            return S_ISREG(stat.st_mode) and stat.st_size == studip_file.size and stat.st_mtime == timestamp
        except FileNotFoundError:
            return False

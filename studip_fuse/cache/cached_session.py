import json
import os
import time
from stat import S_ISREG

import aiofiles.os as aio_os
import attr

from studip_api.async_delay import DeferredTask, DelayLatch, await_idle
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

        self._persist_caches_task = DeferredTask(
            run=self.save_model, trigger_latch=DelayLatch(sleep_fun=await_idle), trigger_delay=60 * 5)

        self.parser.SemesterFactory = Semester.get_or_create
        self.parser.CourseFactory = Course.get_or_create
        self.parser.FileFactory = File.get_or_create
        self.parser.FolderFactory = Folder.get_or_create

        for func in (self.get_semesters, self.get_courses, self.get_course_files, self.get_folder_files):
            func._may_create = self.circuit_breaker.may_create
            func._exception_handler = self.circuit_breaker.exception_handler

    async def close(self):
        await self._persist_caches_task.finalize()
        await super().close()

    async def save_model(self, path=None):
        if not path:
            path = os.path.join(self.cache_dir, "model_data.json")

        # this is an async function, preventing all other async functions from modifying data while generating the obj
        start = time.perf_counter()
        data = ModelObjectMeta.export_all_data()
        for func in (self.get_semesters, self.get_courses, self.get_course_files, self.get_folder_files):
            data[func.__name__] = func.export_cache()
        delta = time.perf_counter() - start

        def save(obj_data):
            with open(path, "wt") as f:
                json.dump(obj_data, f)

        await self._loop.call_soon_threadsafe(save, data)

        return "stored, took %ss" % delta

    async def load_model(self, update=False, path=None):
        if not path:
            path = os.path.join(self.cache_dir, "model_data.json")

        def load():
            with open(path, "rt") as f:
                return json.load(f)

        data = await self._loop.call_soon_threadsafe(load)

        # this is an async function, preventing all other async functions from modifying data while loading the obj
        start = time.perf_counter()
        func_imports = [(func, data.pop(func.__name__)) for func in
                        (self.get_semesters, self.get_courses, self.get_course_files, self.get_folder_files)]
        ModelObjectMeta.import_all_data(data, update)
        for func, fun_data in func_imports:
            func.import_cache(fun_data, update, create_future=self._loop.create_future)
        delta = time.perf_counter() - start

        return "loaded, took %ss" % delta

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
        res = await super().get_semesters()
        self._persist_caches_task.defer()
        return res

    @cached_task(cache_class=ModelGetterCache)
    async def get_courses(self, semester):
        res = await super().get_courses(semester)
        self._persist_caches_task.defer()
        return res

    @cached_task(cache_class=ModelGetterCache)
    async def get_course_files(self, course):
        res = await super().get_course_files(course)
        self._persist_caches_task.defer()
        return res

    @cached_task(cache_class=ModelGetterCache)
    async def get_folder_files(self, folder):
        res = await super().get_folder_files(folder)
        self._persist_caches_task.defer()
        return res

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

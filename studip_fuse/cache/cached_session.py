import functools
import os
import time
from stat import S_ISREG
from typing import Any, Dict

import aiofiles.os as aio_os
import attr
import cattr
from attr import Factory

from studip_api.async_delay import DeferredTask, DelayLatch, await_idle
from studip_api.downloader import Download
from studip_api.model import Course, File, ModelClass, Semester, register_datetime_converter, register_forwardref_converter, register_model_converter
from studip_api.session import StudIPSession, log
from studip_fuse.cache.async_cache import AsyncDownloadCache, AsyncModelCache, is_permanent_exception
from studip_fuse.cache.circuit_breaker import NetworkCircuitBreaker

CACHED_GETTERS = ("get_semesters", "get_courses", "get_course_root_file", "get_folder_files")
TYPE_REGISTRY = {"Semester": Semester, "Course": Course, "File": File}


@attr.s()
class SessionModelRepo(object):
    semesters = attr.ib(type=Dict[str, Semester], default=Factory(dict))
    courses = attr.ib(type=Dict[str, Course], default=Factory(dict))
    files = attr.ib(type=Dict[str, File], default=Factory(dict))

    def get_type_dict(self, t):
        return {Semester: self.semesters, Course: self.courses, File: self.files}[t]

    def get(self, _type, _id):
        return self.get_type_dict(_type)[_id]

    def put(self, _type, _value):
        self.get_type_dict(_type)[_value.id] = _value


@attr.s(hash=False, str=False, repr=False)
class CachedStudIPSession(StudIPSession):
    cache_dir = attr.ib(default=None, validator=attr.validators.instance_of(str))  # type: str
    circuit_breaker = attr.ib(default=Factory(NetworkCircuitBreaker))  # type: NetworkCircuitBreaker

    _persist_caches_task = attr.ib(init=False, repr=False, default=None)  # type: DeferredTask
    _model_data_repo = attr.ib(init=False, repr=False, default=Factory(SessionModelRepo))  # type: SessionModelRepo

    def __attrs_post_init__(self):
        self._persist_caches_task = DeferredTask(
            run=self.save_model, trigger_latch=DelayLatch(sleep_fun=await_idle), trigger_delay=60 * 5)

        base_converter = lambda: register_forwardref_converter(register_datetime_converter(cattr.Converter()))
        model_ref_converter = lambda: register_model_converter(
            base_converter(), base_converter(), self.structure_model_class)
        # self.model_converter, ref_converter = model_ref_converter()
        # self.cache_converter = register_cachevalue_converter(
        #     model_ref_converter()[1], type_registry=TYPE_REGISTRY)
        self.parser.converter, _ = model_ref_converter()

        super().__attrs_post_init__()

        def may_attempt(attempts_history, **context):
            if len(attempts_history) > 0 and is_permanent_exception(attempts_history[-1]):
                return False
            else:
                return self.circuit_breaker.allow_request()

        # FIXME cached instances of ModelClass won't be updated
        for getter in CACHED_GETTERS:
            wrapped = getattr(self, getter)
            wrapper = AsyncModelCache(
                wrapped_function=wrapped, may_attempt=may_attempt,
                on_new_value_fetched=lambda *_, **__: self._persist_caches_task.defer()
            )
            functools.update_wrapper(wrapper, wrapped)
            setattr(self, getter, wrapper)
        self.download_file_contents = AsyncDownloadCache(
            self.download_file_contents, may_attempt=may_attempt
        )

    async def close(self):
        await self._persist_caches_task.finalize()
        await super().close()

    def structure_model_class(self, mc_data: Any, t) -> ModelClass:
        if isinstance(mc_data, t):
            return mc_data
        elif isinstance(mc_data, str):
            return self._model_data_repo.get(t, mc_data)
        else:
            obj = self.parser.converter.structure_attrs_fromdict(mc_data, t)
            self._model_data_repo.put(t, obj)
            return obj

    async def save_model(self, path=None):
        if not path:
            path = os.path.join(self.cache_dir, "model_data.json")

        # TODO implement
        # def save(obj_data):
        #     with open(path, "wt") as f:
        #         json.dump(obj_data, f)
        #
        # data = {"model": self.model_converter.unstructure(self._model_data_repo)}
        # for getter in CACHED_GETTERS:
        #     data[getter] = self.cache_converter.unstructure(getattr(self, getter).cache)
        # await self.loop.run_in_executor(None, save, data)

    async def load_model(self, update=False, path=None):
        if not path:
            path = os.path.join(self.cache_dir, "model_data.json")

        # def load():
        #     with open(path, "rt") as f:
        #         return json.load(f)
        #
        # data = await self.loop.run_in_executor(None, load)
        # self._model_data_repo.update(self.model_converter.structure(data["model"], SessionModelRepo))
        # for getter in CACHED_GETTERS:
        #     getattr(self, getter).update(self.cache_converter.structure(data[getter], AbstractCache))

    async def download_file_contents(self, studip_file, local_dest=None, chunk_size=1024 * 256):
        # FIXME add explicit calls to circuit breaker to individual download parts, potentially allowing retrying failed parts
        if not local_dest:
            local_dest = os.path.join(self.cache_dir, studip_file.id)

        if await self.has_cached_download(studip_file, local_dest):
            log.info("Re-using existing file for download %s -> %s", studip_file, local_dest)
            download = Download(self.ahttp, self.get_download_url(studip_file), local_dest, chunk_size)
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

import asyncio
import time
from os import path
from stat import S_ISREG

import aiofiles.os as aio_os
import attr

from studip_api.downloader import Download
from studip_api.session import StudIPSession, log
from studip_fuse.cache import cached_future_validator, cached_task


def cached_download_validator(key, value):
    if asyncio.isfuture(value) and value.done() and not value.exception() and not value.cancelled():
        result = value.result()
        if isinstance(result, Download) and cached_future_validator(key, result.completed) is None:
            return None

    return cached_future_validator(key, value)


@attr.s(hash=False)
class CachedStudIPSession(StudIPSession):
    cache_dir = attr.ib()  # type: str

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

    @cached_task(cached_value_validator=cached_download_validator)
    async def download_file_contents(self, studip_file, local_dest=None, chunk_size=1024 * 256):
        if not local_dest:
            local_dest = path.join(self.cache_dir, studip_file.id)

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

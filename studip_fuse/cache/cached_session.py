import functools
import os
import time

import attr

from studip_api.session import StudIPSession
from studip_fuse.cache import schedule_task


@attr.s(hash=False)
class CachedStudIPSession(StudIPSession):
    cache_dir = attr.ib()  # type: str

    @functools.lru_cache()
    @schedule_task()
    async def get_semesters(self):
        return await super().get_semesters()

    @functools.lru_cache()
    @schedule_task()
    async def get_courses(self, semester):
        return await super().get_courses(semester)

    @functools.lru_cache()
    @schedule_task()
    async def get_course_files(self, course):
        return await super().get_course_files(course)

    @functools.lru_cache()
    @schedule_task()
    async def get_folder_files(self, folder):
        return await super().get_folder_files(folder)

    @functools.lru_cache()
    @schedule_task()
    async def download_file_contents(self, file, dest=None, chunk_size=1024 * 256):
        if not dest:
            dest = os.path.join(self.cache_dir, file.id)

        # check integrity of existing paths (file with id exists, same size, same change date) and reuse them
        if os.path.isfile(dest):
            timestamp = time.mktime(file.changed.timetuple())
            stat = os.stat(dest)
            if stat.st_size == file.size and stat.st_mtime == timestamp:
                return dest

        return await super().download_file_contents(file, dest)

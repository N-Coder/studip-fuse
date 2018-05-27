import asyncio
import logging
import os
import time
import warnings
from concurrent.futures import Executor
from typing import AsyncGenerator, Dict, List, Union

import attr
from async_generator import async_generator, yield_
from requests import Session as HTTPSession
from requests.auth import HTTPBasicAuth
from studip_api.downloader import Download
from studip_api.model import Course, File, Semester

log = logging.getLogger(__name__)


@async_generator
async def studip_iter(get_next, start, max_total=None) -> AsyncGenerator[Dict]:
    endpoint = start
    last_seen = None
    limit = max_total

    while last_seen is None or last_seen < limit:
        json = get_next(endpoint)
        last_seen = int(json["pagination"]["last_seen"])
        total = int(json["pagination"]["total"])
        limit = min(total, limit)

        for value in json["collection"]:
            await yield_(value)

        endpoint = json["pagination"]["next"]


@attr.s(hash=False, str=False, repr=False)
class StudIPSession(object):
    studip_base = attr.ib()  # type: str
    loop = attr.ib(default=None)  # type: asyncio.AbstractEventLoop
    executor = attr.ib(default=None)  # type: Executor
    http = attr.ib(default=None)  # type: HTTPSession

    async def __aenter__(self):
        if not self.loop:
            self.loop = asyncio.get_event_loop()
        if not self.http:
            self.http = HTTPSession()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    async def close(self):
        await self.loop.run_in_executor(self.executor, self.http.close())

    def _studip_url(self, url):
        return self.studip_base + url

    async def _studip_json_req(self, endpoint):
        return await self.loop.run_in_executor(
            self.executor,
            lambda: self.http.get(self._studip_url(endpoint)).json())

    async def do_login(self, username, password):
        # TODO add login methods
        self.http.auth = HTTPBasicAuth(username, password)
        assert (await self._studip_json_req("user"))["name"] == "username"

        discovery = await self._studip_json_req("discovery")
        for path in [
            "/user",
            "/semesters",
            "/user/:user_id/courses",
            "/course/:course_id",
            "/course/:course_id/top_folder",
            "/folder/:folder_id",
            "/file/:file_ref_id",
            "/file/:file_ref_id/download"
        ]:
            assert path in discovery
            assert "get" in discovery[path]

    async def get_user(self):
        return await self._studip_json_req("user")

    async def get_semesters(self) -> AsyncGenerator[Dict]:
        return await studip_iter(self._studip_json_req, "semesters")

    async def get_courses(self, semester: Semester) -> AsyncGenerator[Dict]:
        return await studip_iter(self._studip_json_req, "user/%s/courses?semester=%s" %
                                 (semester["id"], (await self.get_user())["id"]))

    async def get_course_root_file(self, course: Course) -> Dict:
        return await self._studip_json_req("/course/%s/top_folder" % course["id"])

    async def get_folder_files(self, folder: File) -> AsyncGenerator[Dict]:
        return await studip_iter(self._studip_json_req("/folder/%s" % folder["id"]))

    async def download_file_contents(self, studip_file: File, local_dest: str = None,
                                     chunk_size: int = 1024 * 256) -> Download:

        async def on_completed(download, result: Union[List[range], Exception]):
            if isinstance(result, Exception):
                log.warning("Download %s -> %s failed", studip_file, local_dest, exc_info=True)
            else:
                log.info("Completed download %s -> %s", studip_file, local_dest)

                val = 0
                for r in result:
                    if not r.start <= val:
                        warnings.warn("Non-connected ranges from Download %s: %s" % (download, result))
                    val = r.stop
                if val != download.total_length:
                    warnings.warn("Length of downloaded data doesn't equal length reported by Stud.IP for Download %s: %s"
                                  % (download, result))

                if studip_file.changed:
                    timestamp = time.mktime(studip_file.changed.timetuple())
                    await self.loop.run_in_executor(None, os.utime, local_dest, (timestamp, timestamp))
                else:
                    log.warning("Can't set timestamp of file %s :: %s, because the value wasn't loaded from Stud.IP",
                                studip_file, local_dest)

                return result

        log.info("Starting download %s -> %s", studip_file, local_dest)
        try:
            download = Download(self.ahttp, self._studip_url("file/%s/download" % studip_file), local_dest, chunk_size)
            download.on_completed.append(on_completed)
            await download.start()
        except:
            log.warning("Download %s -> %s could not be started", studip_file, local_dest, exc_info=True)
            raise
        return download

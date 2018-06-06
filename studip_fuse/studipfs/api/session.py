import asyncio
import functools
import logging
import os
import re
import time
import warnings
from typing import Dict, List, Mapping, Tuple, Union

import attr
from async_generator import async_generator, yield_, yield_from_
from pyrsistent import freeze, pvector
from requests import Session as HTTPSession
from requests.auth import HTTPBasicAuth
from studip_api.downloader import Download

log = logging.getLogger(__name__)


# TODO move, add drop-in caching
class AsyncHTTPSession(HTTPSession):
    def __init__(self, loop=None, executor=None, *args, **kwargs):
        self.loop = loop or asyncio.get_event_loop()
        self.executor = executor
        super(AsyncHTTPSession, self).__init__(*args, **kwargs)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.close()

    def close(self):
        return self.loop.run_in_executor(self.executor, super(AsyncHTTPSession, self).close)

    async def request(self, *args, **kwargs):
        resp = await self.loop.run_in_executor(self.executor, functools.partial(super(AsyncHTTPSession, self).request, *args, **kwargs))
        resp.raise_for_status()
        return resp


@async_generator
async def studip_iter(get_next, start, max_total=None):  # -> AsyncGenerator[Dict, None]
    endpoint = start
    last_seen = None
    limit = max_total

    while last_seen is None or last_seen < limit:
        json = await get_next(endpoint)
        last_seen = int(json["pagination"]["offset"]) + len(json["collection"])
        total = int(json["pagination"]["total"])
        limit = min(total, limit or total)

        coll = json["collection"]
        if isinstance(coll, Mapping):
            coll = coll.values()
        for value in coll:
            await yield_(value)

        try:
            endpoint = json["pagination"]["links"]["next"]
        except KeyError:
            break


@attr.s(hash=False, str=False, repr=False)
class StudIPSession(object):
    studip_base = attr.ib()  # type: str
    http = attr.ib()  # type: HTTPSession

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    async def close(self):
        await self.http.close()

    def _studip_url(self, url):
        prefix = "api.php"
        index = url.find(prefix)
        if index >= 0:
            url = url[index + len(prefix):]
        while url.startswith('/'):
            url = url[1:]
        return self.studip_base + url

    async def _studip_json_req(self, endpoint):
        resp = await self.http.get(self._studip_url(endpoint))
        return freeze(resp.json())

    async def do_login(self, username, password):
        # TODO add login methods
        self.http.auth = HTTPBasicAuth(username, password)
        user_data = await self._studip_json_req("user")
        assert user_data["username"] == username

        discovery = await self._studip_json_req("discovery")
        for path in [
            "/user",
            "/studip/settings",
            "/semesters",
            "/user/:user_id/courses",
            # "/course/:course_id",
            # "/course/:course_id/files",
            "/course/:course_id/top_folder",
            "/folder/:folder_id",
            # "/file/:file_ref_id",
            # "/file/:file_id/content",
            "/file/:file_ref_id/download"
        ]:
            assert path in discovery
            assert "get" in discovery[path]

    async def get_user(self):
        return await self._studip_json_req("user")

    async def get_settings(self):
        return await self._studip_json_req("studip/settings")

    @async_generator
    async def get_semesters(self):  # -> AsyncGenerator[Dict, None]:
        await yield_from_(studip_iter(self._studip_json_req, "semesters"))

    @async_generator
    async def get_courses(self, semester):  # -> AsyncGenerator[Dict, None]:
        semesters = {self.extract_id(semester): semester async for semester in self.get_semesters()}
        settings = await self.get_settings()
        user = await self.get_user()

        url = "user/%s/courses?semester=%s" % (self.extract_id(user), self.extract_id(semester))
        async for course in studip_iter(self._studip_json_req, url):
            course_ev = course.evolver()
            if course.get("start_semester", None):
                start_semester = semesters[self.extract_id(course["start_semester"])]
                course_ev["start_semester"] = start_semester
                course_ev["start_date"] = start_semester["begin"]
            if course.get("end_semester", None):
                end_semester = semesters[self.extract_id(course["end_semester"])]
                course_ev["end_semester"] = end_semester
                course_ev["end_date"] = end_semester["end"]

            type_data = settings["SEM_TYPE"][course["type"]]
            class_data = settings["SEM_CLASS"][type_data["class"]]

            course_ev["type-nr"] = course["type"]
            course_ev["type"] = type_data["name"]
            course_ev["class"] = class_data["name"]

            await yield_(course_ev.persistent())

    async def get_course_root_folder(self, course) -> Tuple[Dict, List, List]:
        folder = await self._studip_json_req("/course/%s/top_folder" % self.extract_id(course))
        return self.return_folder(folder)

    async def get_folder_details(self, parent) -> Tuple[Dict, List, List]:
        folder = await self._studip_json_req("/folder/%s" % self.extract_id(parent))
        return self.return_folder(folder)

    async def get_file_details(self, parent) -> Tuple[Dict, List, List]:
        file = await self._studip_json_req("/file/%s" % self.extract_id(parent))
        if file.get("id", None) != file.get("file_id", None):
            warnings.warn("File has non-matching `(file_)id`s: %s" % file)
        return file

    def return_folder(self, folder):
        subfolders = folder.get("subfolders", [])
        file_refs = folder.get("file_refs", [])

        folder_ev = folder.evolver()
        if "subfolders" in folder_ev:
            folder_ev.remove("subfolders")
        if "file_refs" in folder_ev:
            folder_ev.remove("file_refs")
        folder_ev.set("subfolder_count", len(subfolders))
        folder_ev.set("file_count", len(file_refs))

        return folder_ev.persistent(), \
               pvector(self.extract_id(f) for f in subfolders), \
               pvector(self.extract_id(f) for f in file_refs)

    def extract_id(self, val):
        if isinstance(val, Mapping):
            if "id" in val:
                return self.extract_id(val["id"])
            if "course_id" in val:
                return self.extract_id(val["course_id"])
            if "user_id" in val:
                return self.extract_id(val["user_id"])
        elif isinstance(val, str):
            m = re.fullmatch("(.*/)?(?P<id>[a-z0-9]{31,32})(\?.*)?", val.lower())
            if m:
                # print(len(m.group("id")), val, m.group("id"))
                return m.group("id")

        raise ValueError("can't extract id from %s '%s'" % (type(val), val))

    async def download_file_contents(self, studip_file, local_dest: str = None,
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

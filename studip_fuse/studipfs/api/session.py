import logging
import re
import warnings
from typing import AsyncGenerator, List, Mapping, Tuple

import attr
from async_generator import async_generator, yield_
from pyrsistent import freeze, pmap, pvector

from studip_fuse.aioutils.interface import FileStore, HTTPSession

log = logging.getLogger(__name__)
Dict = pmap


@async_generator
async def studip_iter_(get_next, start, max_total=None):
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


def studip_iter(get_next, start, max_total=None) -> AsyncGenerator[Dict, None]:  # fix type information for PyCharm
    # noinspection PyTypeChecker
    return studip_iter_(get_next, start, max_total)


# Old docs: https://docs.studip.de/develop/Entwickler/RESTAPI
# New docs: https://hilfe.studip.de/develop/Entwickler/RESTAPI

@attr.s(hash=False, str=False, repr=False)
class StudIPSession(object):
    studip_base = attr.ib()  # type: str
    http = attr.ib()  # type: HTTPSession
    storage = attr.ib()  # type: FileStore

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    async def close(self):
        pass  # no clean-up needed, http and storage are closed externally by the loop impl

    def _studip_url(self, url):
        prefix = "api.php"
        index = url.find(prefix)
        if index >= 0:
            url = url[index + len(prefix):]
        while url.startswith('/'):
            url = url[1:]
        return self.studip_base + url

    async def _studip_json_req(self, endpoint) -> Dict:
        resp = await self.http.get(self._studip_url(endpoint))  # TODO close request object
        return freeze(resp.json())  # TODO this must probably be awaited, too

    @classmethod
    def with_middleware(cls, async_annotation, agen_annotation, download_annotation, name="GenericMiddlewareStudIPSession"):
        return type(name, (cls,), {
            "do_login": async_annotation(cls.do_login),
            "get_user": async_annotation(cls.get_user),
            "get_settings": async_annotation(cls.get_settings),
            "get_course_root_folder": async_annotation(cls.get_course_root_folder),
            "get_folder_details": async_annotation(cls.get_folder_details),
            "get_file_details": async_annotation(cls.get_file_details),

            "get_semesters": agen_annotation(cls.get_semesters),
            "get_courses": agen_annotation(cls.get_courses),

            "retrieve_file": download_annotation(cls.retrieve_file),  # FIXME this should be filestore middleware
        })

    async def do_login(self, username, password):
        # TODO add login methods
        self.http.auth = (username, password)
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

    async def get_user(self) -> Dict:
        return await self._studip_json_req("user")

    async def get_settings(self) -> Dict:
        return await self._studip_json_req("studip/settings")

    def get_semesters(self) -> AsyncGenerator[Dict, None]:
        return studip_iter(self._studip_json_req, "semesters")

    @async_generator
    async def get_courses_(self, semester):
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

    def get_courses(self, semester) -> AsyncGenerator[Dict, None]:  # fix type information for PyCharm
        # noinspection PyTypeChecker
        return self.get_courses_(semester)

    async def get_course_root_folder(self, course) -> Tuple[Dict, List, List]:
        folder = await self._studip_json_req("/course/%s/top_folder" % self.extract_id(course))
        return self.return_folder(folder)

    async def get_folder_details(self, parent) -> Tuple[Dict, List, List]:
        folder = await self._studip_json_req("/folder/%s" % self.extract_id(parent))
        return self.return_folder(folder)

    async def get_file_details(self, parent) -> Dict:
        file = await self._studip_json_req("/file/%s" % self.extract_id(parent))
        if file.get("id", None) != file.get("file_id", None):
            warnings.warn("File has non-matching `(file_)id`s: %s" % file)
        return file

    def return_folder(self, folder) -> Tuple[Dict, List, List]:
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

    async def retrieve_file(self, file):
        return await self.storage.retrieve(
            uid=file["id"],  # TODO should uid be the file revision id or the (unchangeable) id of the file
            url=self._studip_url("file/%s/download" % file["id"]),  # this requires "id", not "file_id"
            overwrite_created=file["chdate"],  # TODO or file["mkdate"] # TODO datetime.fromtimestamp(...) here?
            expected_size=file["size"]
        )

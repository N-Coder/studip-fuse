import logging
import re
import warnings
from datetime import datetime
from typing import AsyncGenerator, List, Mapping, Tuple

import attr
from async_generator import async_generator, yield_
from pyrsistent import freeze, pvector as FrozenList
from yarl import URL

from studip_fuse.studipfs.api.aiointerface import FrozenDict, HTTPClient

REQUIRED_API_ENDPOINTS = [
    "discovery",
    "user",
    "studip/settings",
    "studip/content_terms_of_use_list",
    "studip/file_system/folder_types",
    "extern/coursetypes",
    "semesters",
    "user/:user_id/courses",
    # "course/:course_id",
    # "course/:course_id/files",
    "course/:course_id/top_folder",
    "folder/:folder_id",
    # "file/:file_ref_id",
    # "file/:file_id/content",
    "file/:file_ref_id/download"
]

log = logging.getLogger(__name__)


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


def studip_iter(get_next, start, max_total=None) -> AsyncGenerator[FrozenDict, None]:  # fix type information for PyCharm
    # noinspection PyTypeChecker
    return studip_iter_(get_next, start, max_total)


def append_base_url_slash(value):
    value = URL(value)
    if not value.path.endswith("/"):
        log.warning("StudIP API %s must end with a slash. Appending '/' to make path concatenation work correctly.", repr(value))
        value = value.with_path(value.path + "/")
    return value


# Old docs: https://docs.studip.de/develop/Entwickler/RESTAPI
# New docs: https://hilfe.studip.de/develop/Entwickler/RESTAPI

@attr.s(hash=False, str=False, repr=False)
class StudIPSession(object):
    studip_base = attr.ib(converter=append_base_url_slash)  # type: URL
    http = attr.ib()  # type: HTTPClient

    studip_course_type = attr.ib(init=False)  # type: FrozenDict
    studip_course_class = attr.ib(init=False)  # type: FrozenDict
    studip_file_tou = attr.ib(init=False)  # type: FrozenDict
    studip_folder_types = attr.ib(init=False)  # type: FrozenDict
    studip_course_types = attr.ib(init=False)  # type: FrozenDict

    def studip_url(self, url):
        return self.studip_base.join(URL(url))

    async def get_studip_json(self, url):
        url = URL(url)
        if not url.path.startswith("/") and url.path not in REQUIRED_API_ENDPOINTS:
            if not any(url.path.startswith(endpoint) for endpoint in REQUIRED_API_ENDPOINTS):
                log.warning("Relative path %s is not in required paths, which are checked at startup." % url)
        return await self.http.get_json(self.studip_url(url))

    @classmethod
    def with_middleware(cls, async_annotation, agen_annotation, download_annotation, name="GenericMiddlewareStudIPSession"):
        return type(name, (cls,), {
            "get_user": async_annotation(cls.get_user),
            "get_settings": async_annotation(cls.get_settings),
            "get_course_root_folder": async_annotation(cls.get_course_root_folder),
            "get_folder_details": async_annotation(cls.get_folder_details),
            "get_file_details": async_annotation(cls.get_file_details),

            "get_semesters": agen_annotation(cls.get_semesters),
            "get_courses": agen_annotation(cls.get_courses),

            "retrieve_file": download_annotation(cls.retrieve_file),
        })

    async def check_login(self, username):
        user_data = await self.get_studip_json("user")
        assert user_data["username"] == username

        discovery = await self.get_studip_json("discovery")
        for path in REQUIRED_API_ENDPOINTS:
            path = "/" + path
            assert path in discovery
            assert "get" in discovery[path]

        settings = await self.get_studip_json("studip/settings")

        self.studip_course_type = {}
        for key, value in settings["SEM_TYPE"].items():
            value = value.set("id", int(key))
            self.studip_course_type[int(key)] = value
            self.studip_course_type[str(key)] = value
            self.studip_course_type[str(value["name"])] = value
        self.studip_course_type = freeze(self.studip_course_type)

        self.studip_course_class = {}
        for key, value in settings["SEM_CLASS"].items():
            value = value.set("id", int(key))
            self.studip_course_class[int(key)] = value
            self.studip_course_class[str(key)] = value
            self.studip_course_class[str(value["name"])] = value
        self.studip_course_class = freeze(self.studip_course_class)

        self.studip_file_tou = {}
        async for tou in studip_iter(self.get_studip_json, "studip/content_terms_of_use_list"):
            self.studip_file_tou[tou["id"]] = tou  # id is a str like UNDEF_LICENSE
        self.studip_file_tou = freeze(self.studip_file_tou)

        self.studip_folder_types = await self.get_studip_json("studip/file_system/folder_types")

    async def get_user(self) -> FrozenDict:
        return await self.get_studip_json("user")

    async def get_settings(self) -> FrozenDict:
        return await self.get_studip_json("studip/settings")

    def get_semesters(self) -> AsyncGenerator[FrozenDict, None]:
        return studip_iter(self.get_studip_json, "semesters")

    @async_generator
    async def get_courses_(self, semester):
        semesters = {self.extract_id(semester): semester async for semester in self.get_semesters()}
        settings = await self.get_settings()
        user = await self.get_user()

        url = "user/%s/courses?semester=%s" % (self.extract_id(user), self.extract_id(semester))
        async for course in studip_iter(self.get_studip_json, url):
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

    def get_courses(self, semester) -> AsyncGenerator[FrozenDict, None]:  # fix type information for PyCharm
        # noinspection PyTypeChecker
        return self.get_courses_(semester)

    async def get_course_root_folder(self, course) -> Tuple[FrozenDict, List, List]:
        folder = await self.get_studip_json("course/%s/top_folder" % self.extract_id(course))
        return self.return_folder(folder)

    async def get_folder_details(self, parent) -> Tuple[FrozenDict, List, List]:
        folder = await self.get_studip_json("folder/%s" % self.extract_id(parent))
        return self.return_folder(folder)

    async def get_file_details(self, parent) -> FrozenDict:
        file = await self.get_studip_json("file/%s" % self.extract_id(parent))
        if file.get("id", None) != file.get("file_id", None):
            warnings.warn("File has non-matching `(file_)id`s: %s" % file)
        return file

    def return_folder(self, folder) -> Tuple[FrozenDict, List, List]:
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
               FrozenList(self.extract_id(f) for f in subfolders), \
               FrozenList(self.extract_id(f) for f in file_refs)

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
        return await self.http.retrieve(
            uid=file["id"],  # TODO should uid be the file revision id or the (unchangeable) id of the file
            url=self.studip_url("file/%s/download" % file["id"]),  # this requires "id", not "file_id"
            overwrite_created=datetime.fromtimestamp(int(file["chdate"])),  # TODO or file["mkdate"]
            expected_size=int(file["size"])
        )

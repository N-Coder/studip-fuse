import logging
import re
import warnings
from datetime import datetime
from typing import AsyncGenerator, List, Mapping, Tuple

import attr
from aiohttp import ClientResponseError
from aiohttp.web_exceptions import HTTPClientError, HTTPForbidden, HTTPUnauthorized
from async_generator import async_generator, yield_
from pyrsistent import freeze, pvector as FrozenList
from yarl import URL

from studip_fuse.studipfs.api.aiointerface import FrozenDict, StudIPSession

__all__ = ["REQUIRED_API_ENDPOINTS", "StudIPAPISession"]
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
    "file/:file_ref_id",
    # "file/:file_id/content",
    "file/:file_ref_id/download"
]
ENDPOINT_REGEXES = ["^" + re.sub(":[^/]+", ".*", e) for e in REQUIRED_API_ENDPOINTS]

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


@async_generator
async def asyncified_iter_(it):
    for val in it:
        await yield_(val)


def asyncified_iter(it) -> AsyncGenerator:  # fix type information for PyCharm
    # noinspection PyTypeChecker
    return asyncified_iter_(it)


# Old docs: https://docs.studip.de/develop/Entwickler/RESTAPI
# New docs: https://hilfe.studip.de/develop/Entwickler/RESTAPI

@attr.s(hash=False, str=False, repr=False)
class StudIPAPISession(StudIPSession):
    studip_settings = attr.ib(init=False, default=None)  # type: FrozenDict
    studip_course_type = attr.ib(init=False)  # type: FrozenDict # map for [int(id), str(id) and name] -> {'id': 21, 'name': 'Workshop', 'class': '3'}
    studip_course_class = attr.ib(init=False)  # type: FrozenDict # map for [int(id), str(id) and name] -> {'id': 4, 'name': 'Studien-/Arbeitsgruppen', ...}
    studip_file_tou = attr.ib(init=False)  # type: FrozenDict
    studip_folder_type = attr.ib(init=False)  # type: FrozenDict
    studip_semester = attr.ib(init=False)  # type: FrozenDict

    async def get_studip_json(self, url):
        url = URL(url)
        if not url.path.startswith("/") and url.path not in REQUIRED_API_ENDPOINTS:
            if not any([re.match(p, url.path) for p in ENDPOINT_REGEXES]):
                warnings.warn("Relative path %s is not in required paths, which are checked at startup." % url)
        return await self.http.get_json(self.studip_url(url))

    async def check_login(self, username=None):
        try:
            user_data = await self.get_studip_json("user")
            if username and user_data["username"] != username:
                raise RuntimeError("Requested to login as %s, but this session belongs to %s. "
                                   "Did you use the wrong OAuth session token?" % (username, user_data["username"]))

            discovery = await self.get_studip_json("discovery")
            for path in REQUIRED_API_ENDPOINTS:
                path = "/" + path
                if path not in discovery or "get" not in discovery[path]:
                    raise RuntimeError("Required API route %s is not available on your Stud.IP instance at %s."
                                       % (path, self.studip_base))

            return user_data
        except (HTTPClientError, ClientResponseError) as e:
            if e.status == HTTPUnauthorized.status_code:
                raise RuntimeError("Login failed, please check your credentials and try again.") from e
            elif e.status == HTTPForbidden.status_code:
                raise RuntimeError("The required Stud.IP API was disabled by the administrator of your instance at %s."
                                   % self.studip_base) from e
            else:
                raise

    async def prefetch_globals(self):
        self.studip_settings = await self.get_studip_json("studip/settings")

        self.studip_course_type = {}
        for key, value in self.studip_settings["SEM_TYPE"].items():
            value = value.set("id", int(key))
            self.studip_course_type[int(key)] = value
            self.studip_course_type[str(key)] = value
            self.studip_course_type[str(value["name"])] = value
        self.studip_course_type = freeze(self.studip_course_type)

        self.studip_course_class = {}
        for key, value in self.studip_settings["SEM_CLASS"].items():
            value = value.set("id", int(key))
            self.studip_course_class[int(key)] = value
            self.studip_course_class[str(key)] = value
            self.studip_course_class[str(value["name"])] = value
        self.studip_course_class = freeze(self.studip_course_class)

        self.studip_file_tou = {}
        async for tou in studip_iter(self.get_studip_json, "studip/content_terms_of_use_list"):
            self.studip_file_tou[tou["id"]] = tou  # id is a str like UNDEF_LICENSE
        self.studip_file_tou = freeze(self.studip_file_tou)

        self.studip_folder_type = await self.get_studip_json("studip/file_system/folder_types")

        self.studip_semester = {}
        async for sem in studip_iter(self.get_studip_json, "semesters"):
            self.studip_semester[self.__extract_id(sem)] = sem
        self.studip_semester = freeze(self.studip_semester)

    async def get_instance_name(self):
        if not self.studip_settings:
            self.studip_settings = await self.get_studip_json("studip/settings")

        return "%s Stud.IP v%s running at %s" % \
               (self.studip_settings["UNI_NAME_CLEAN"], await self.get_version(), self.studip_base)

    async def get_user(self):
        return await self.get_studip_json("user")

    def get_semesters(self):
        return asyncified_iter(sorted(self.studip_semester.values(), key=lambda s: s["begin"]))

    @async_generator
    async def __get_courses(self, semester):
        user = await self.get_user()

        url = "user/%s/courses?semester=%s" % (self.__extract_id(user), self.__extract_id(semester))
        async for course in studip_iter(self.get_studip_json, url):
            course_ev = course.evolver()
            if course.get("start_semester", None):
                start_semester = self.studip_semester[self.__extract_id(course["start_semester"])]
                course_ev["start_semester"] = start_semester
                course_ev["start_date"] = start_semester["begin"]
            if course.get("end_semester", None):
                end_semester = self.studip_semester[self.__extract_id(course["end_semester"])]
                course_ev["end_semester"] = end_semester
                course_ev["end_date"] = end_semester["end"]

            type_data = self.studip_course_type[course["type"]]
            class_data = self.studip_course_class[type_data["class"]]
            course_ev["type_id"] = type_data["id"]
            course_ev["type"] = type_data["name"]
            course_ev["class_id"] = class_data["id"]
            course_ev["class"] = class_data["name"]

            await yield_(course_ev.persistent())

    def get_courses(self, semester):  # fix type information for PyCharm
        # noinspection PyTypeChecker
        return self.__get_courses(semester)

    async def get_course_root_folder(self, course):
        folder = await self.get_studip_json("course/%s/top_folder" % self.__extract_id(course))
        return self.__return_folder(folder)

    async def get_folder_details(self, parent):
        folder = await self.get_studip_json("folder/%s" % self.__extract_id(parent))
        return self.__return_folder(folder)

    async def get_file_details(self, parent):
        file = await self.get_studip_json("file/%s" % self.__extract_id(parent))
        if file.get("id", None) != file.get("file_id", None):
            warnings.warn("File has non-matching `(file_)id`s: %s" % file)
        return file

    def __return_folder(self, folder) -> Tuple[FrozenDict, List, List]:
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
               FrozenList(self.__extract_id(f) for f in subfolders), \
               FrozenList(self.__extract_id(f) for f in file_refs)

    def __extract_id(self, val):
        if isinstance(val, Mapping):
            if "id" in val:
                return self.__extract_id(val["id"])
            if "course_id" in val:
                return self.__extract_id(val["course_id"])
            if "user_id" in val:
                return self.__extract_id(val["user_id"])
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

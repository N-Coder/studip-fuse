import warnings
from abc import ABC, abstractmethod
from collections import namedtuple
from datetime import datetime
from io import FileIO
from queue import Queue
from typing import Any, Callable, Coroutine, Dict, Generic, List, NamedTuple, Optional, Tuple, TypeVar, Union

import attr
from aiofiles.threadpool import AsyncFileIO
from more_itertools import one
from pyrsistent import pmap as FrozenDict
from typing_extensions import AsyncContextManager, AsyncIterator
from yarl import URL

from studip_fuse.avfs.real_path import RealPath

try:
    from typing_extensions import AsyncGenerator
except ImportError:
    _T_co = TypeVar("_T_co", covariant=True)
    _T_contra = TypeVar("_T_contra", contravariant=True)


    class AsyncGenerator(AsyncIterator[_T_co], Generic[_T_co, _T_contra]):
        """similar to the definition in trio-typing for async-generator, but without the dependency on trio and mypy"""
        pass

__all__ = ["Pipeline", "HTTPResponse", "HTTPClient", "Download", "StudIPSession", "LoopSetupResult"] + \
          ["T", "AsyncGenerator", "AsyncContextManager"] + \
          ["OAuth1URLs", "DEFAULT_OAUTH1_URLS"]
T = TypeVar('T')

LoopSetupResult = NamedTuple("LoopSetupResult", [
    ("loop_stop_fn", Callable),
    ("loop_run_fn", Callable),
    ("root_rp", RealPath),
])


class Pipeline(ABC, Generic[T]):
    @abstractmethod
    def put(self, item: T):
        pass

    @abstractmethod
    def drain(self) -> AsyncGenerator[T, None]:
        pass

    @abstractmethod
    def add_processor(self, func: Callable[[T, "Queue[T]"], Coroutine[Any, Any, None]]):
        pass


HTTPResponse = NamedTuple("HTTPResponse", [
    ("url", URL),
    ("headers", Dict[str, str]),
    ("content", Union[str, Dict]),
])


class HTTPClient(AsyncContextManager, ABC):
    @classmethod
    def with_middleware(cls, get_json_annotation, auth_annotation, download_annotation, name="GenericMiddlewareHTTPClient"):
        return type(name, (cls,), {
            "get_json": get_json_annotation(cls.get_json),
            "basic_auth": auth_annotation(cls.basic_auth),
            "oauth1_auth": auth_annotation(cls.oauth1_auth),
            "shib_auth": auth_annotation(cls.shib_auth),
            "retrieve": download_annotation(cls.retrieve),
        })

    @abstractmethod
    async def async_result(self, func, *args, **kwargs):
        pass

    @abstractmethod
    async def get_json(self, url) -> FrozenDict:
        pass

    @abstractmethod
    async def get_text(self, url) -> str:
        pass

    # auth = (Method/Strategy x IO Interface x Endpoint URLs x User Credentials)
    @abstractmethod
    async def basic_auth(self, username, password):
        pass

    @abstractmethod
    async def oauth1_auth(self, **kwargs):
        pass

    @abstractmethod
    async def shib_auth(self, start_url, username, password):
        # FIXME shibboleth session expiration
        # url is the starting point of the Shibboleth flow, e.g. self._studip_url("/studip/index.php?again=yes&sso=shib")
        pass

    @abstractmethod
    async def retrieve(self, uid: str, url: Union[str, URL], overwrite_created: Optional[datetime] = None, expected_size: Optional[int] = None) -> "Download":
        # TODO should id be the file revision id or the (unchangeable) id of the file
        pass


@attr.s()
class Download(ABC):
    uid = attr.ib()  # type: str
    url = attr.ib(converter=URL)  # type: URL
    local_path = attr.ib()  # type: str
    total_length = attr.ib()  # type: int
    last_modified = attr.ib()  # type: datetime

    @property
    @abstractmethod
    def is_loading(self) -> bool:
        return False

    @property
    @abstractmethod
    def is_completed(self) -> bool:
        return False

    @property
    @abstractmethod
    def exception(self) -> BaseException:
        pass

    @abstractmethod
    async def start_loading(self, offset=0, length=-1):
        pass

    def is_readable(self, offset=0, length=-1) -> bool:
        if length < 0:
            length = self.total_length - offset
        return self.readable_bytes(offset) >= length

    @abstractmethod
    def readable_bytes(self, offset=0) -> int:
        pass

    @abstractmethod
    async def await_readable(self, offset=0, length=-1, start_loading=False):
        pass

    @abstractmethod
    def open_sync(self, flags=0) -> FileIO:
        """blockingly opens another view into the file"""
        pass

    @abstractmethod
    async def open_async(self, flags=0, loop=None, executor=None) -> AsyncFileIO:
        pass


def append_base_url_slash(value):
    value = URL(value)
    if not value.path.endswith("/"):
        warnings.warn("StudIP API %s must end with a slash. Appending '/' to make path concatenation work correctly.", repr(value))
        value = value.with_path(value.path + "/")
    return value


OAuth1URLs = namedtuple("OAuthURLs", ["access_token", "authorize", "request_token"])
DEFAULT_OAUTH1_PREFIX = "../dispatch.php/api/oauth/"
DEFAULT_OAUTH1_URLS = OAuth1URLs(DEFAULT_OAUTH1_PREFIX + "access_token", DEFAULT_OAUTH1_PREFIX + "authorize", DEFAULT_OAUTH1_PREFIX + "request_token")


@attr.s(hash=False, str=False, repr=False)
class StudIPSession(ABC):
    studip_base = attr.ib(converter=append_base_url_slash)  # type: URL
    http = attr.ib(repr=False)  # type: HTTPClient

    rel_oauth1_urls = attr.ib(default=DEFAULT_OAUTH1_URLS)  # type: OAuth1URLs

    def studip_url(self, url):
        return self.studip_base.join(URL(url))

    @property
    def oauth1_urls(self) -> OAuth1URLs:
        return OAuth1URLs(*(self.studip_url(v) for v in self.rel_oauth1_urls))

    async def get_version(self):
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(await self.http.get_text(self.studip_url("/studip/dispatch.php/siteinfo/")), 'lxml')
        return str(one(soup.find_all(string="Version:")).parent.next_sibling).strip()

    @classmethod
    def with_middleware(cls, async_annotation, agen_annotation, download_annotation, name="GenericMiddlewareStudIPSession"):
        return type(name, (cls,), {
            "get_user": async_annotation(cls.get_user),
            "get_course_root_folder": async_annotation(cls.get_course_root_folder),
            "get_folder_details": async_annotation(cls.get_folder_details),
            "get_file_details": async_annotation(cls.get_file_details),

            "get_semesters": agen_annotation(cls.get_semesters),
            "get_courses": agen_annotation(cls.get_courses),

            "retrieve_file": download_annotation(cls.retrieve_file),
        })

    @abstractmethod
    async def check_login(self, username):
        pass

    @abstractmethod
    async def prefetch_globals(self):
        pass

    @abstractmethod
    async def get_instance_name(self) -> str:
        pass

    @abstractmethod
    async def get_user(self) -> FrozenDict:
        pass

    @abstractmethod
    def get_semesters(self) -> AsyncGenerator[FrozenDict, None]:
        pass

    @abstractmethod
    def get_courses(self, semester) -> AsyncGenerator[FrozenDict, None]:
        pass

    @abstractmethod
    async def get_course_root_folder(self, course) -> Tuple[FrozenDict, List, List]:
        pass

    @abstractmethod
    async def get_folder_details(self, parent) -> Tuple[FrozenDict, List, List]:
        pass

    @abstractmethod
    async def get_file_details(self, parent) -> FrozenDict:
        pass

    @abstractmethod
    async def retrieve_file(self, file) -> Download:
        pass

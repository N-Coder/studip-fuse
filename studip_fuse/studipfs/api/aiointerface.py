from abc import ABC, abstractmethod
from datetime import datetime
from queue import Queue
from typing import Any, Callable, Coroutine, Dict, Generic, NamedTuple, Optional, TypeVar, Union

import attr
from pyrsistent import pmap as FrozenDict
from typing_extensions import AsyncContextManager, AsyncIterator
from yarl import URL

try:
    from typing_extensions import AsyncGenerator
except ImportError:
    _T_co = TypeVar("_T_co", covariant=True)
    _T_contra = TypeVar("_T_contra", contravariant=True)


    class AsyncGenerator(AsyncIterator[_T_co], Generic[_T_co, _T_contra]):
        """similar to the definition in trio-typing for async-generator, but without the dependency on trio and mypy"""
        pass

__all__ = ["Pipeline", "HTTPResponse", "HTTPClient", "Download", "T", "AsyncGenerator", "AsyncContextManager"]
T = TypeVar('T')


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
    async def get_json(self, url) -> FrozenDict:
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

    @abstractmethod
    def exception(self) -> BaseException:
        pass

    @abstractmethod
    async def start_loading(self):
        pass

    @abstractmethod
    async def await_readable(self, offset=0, length=-1):
        pass

from abc import ABC, abstractmethod
from datetime import datetime
from queue import Queue
from typing import Any, AsyncGenerator, Awaitable, Callable, Coroutine, Generic, Iterable, Optional, TypeVar

import attr

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


class AsyncCloseable(object):
    async def close(self):
        pass


class FileStore(ABC, AsyncCloseable):
    @abstractmethod
    async def retrieve(self, uid: str, url: str, overwrite_created: Optional[datetime] = None, expected_size: Optional[int] = None) -> "Download":
        # TODO should id be the file revision id or the (unchangeable) id of the file
        pass


@attr.s()
class Download(ABC):
    uid = attr.ib()  # type: str
    url = attr.ib()  # type: str
    local_path = attr.ib()  # type: str

    @property
    @abstractmethod
    def total_length(self) -> int:
        return -1

    @property
    @abstractmethod
    def is_running(self) -> bool:
        return False

    @property
    @abstractmethod
    def is_completed(self) -> bool:
        return False

    @abstractmethod
    async def start(self):
        pass

    @abstractmethod
    async def await_readable(self, offset=0, length=-1):
        pass


class Request(ABC, AsyncCloseable, Awaitable):
    @property
    def content(self) -> bytes:
        pass

    @property
    def text(self) -> str:
        pass

    def json(self) -> dict:
        pass

    def raise_for_status(self):
        pass

    def iter_content(self) -> Iterable[bytes]:
        pass


class HTTPSession(ABC, AsyncCloseable):
    @abstractmethod
    async def request(self, method, url, **kwargs) -> Request:
        pass

    @abstractmethod
    async def get(self, url, **kwargs) -> Request:
        pass

    @abstractmethod
    async def options(self, url, **kwargs) -> Request:
        pass

    @abstractmethod
    async def head(self, url, **kwargs) -> Request:
        pass

    @abstractmethod
    async def post(self, url, data=None, **kwargs) -> Request:
        pass

    @abstractmethod
    async def put(self, url, data=None, **kwargs) -> Request:
        pass

    @abstractmethod
    async def patch(self, url, data=None, **kwargs) -> Request:
        pass

    @abstractmethod
    async def delete(self, url, **kwargs) -> Request:
        pass

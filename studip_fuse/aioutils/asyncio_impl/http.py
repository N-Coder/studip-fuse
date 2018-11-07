import asyncio
import functools
import logging

import attr
from requests import Session as SyncRequestsHTTPSession

from studip_fuse.aioutils.interface import HTTPSession as IHTTPSession, Request as IRequest

log = logging.getLogger(__name__)


# TODO AsyncRequest as AsyncContextManager vs as Coroutine
# TODO AsyncHTTPSession should have a download() function instead of using predefined Download object in session

@attr.s()
class AsyncioSyncRequestsRequest(IRequest):
    loop = attr.ib()
    executor = attr.ib()
    coro = attr.ib()
    result = attr.ib(default=None, init=False)

    def __await__(self):
        assert not self.result
        self.result = True  # can't catch result here
        return self.coro.__await__()

    async def __aenter__(self):
        assert not self.result
        self.result = await self.coro
        return self.result

    async def __aexit__(self, type, value, traceback):
        return await self.loop.run_in_executor(self.executor, self.result.close)


class AsyncioSyncRequestsHTTPSession(SyncRequestsHTTPSession, IHTTPSession):
    def __init__(self, loop=None, executor=None, *args, **kwargs):
        self.loop = loop or asyncio.get_event_loop()
        self.executor = executor
        self.sync_request = super(AsyncioSyncRequestsHTTPSession, self).request
        super(AsyncioSyncRequestsHTTPSession, self).__init__(*args, **kwargs)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.close()

    def close(self):
        return self.loop.run_in_executor(self.executor, super(AsyncioSyncRequestsHTTPSession, self).close)

    def request(self, *args, **kwargs):
        return AsyncioSyncRequestsRequest(self.loop, self.executor, self.async_request(*args, **kwargs))

    async def async_request(self, *args, **kwargs):
        resp = await self.loop.run_in_executor(self.executor, functools.partial(self.sync_request, *args, **kwargs))
        resp.raise_for_status()
        return resp


class AsyncioSyncRequestsCachedHTTPSession(AsyncioSyncRequestsHTTPSession):
    def __init__(self, *args, **kwargs):
        super(AsyncioSyncRequestsCachedHTTPSession, self).__init__(*args, **kwargs)
        self.sync_request = functools.lru_cache()(self.sync_request)

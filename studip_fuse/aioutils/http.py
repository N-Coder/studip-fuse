import asyncio
import functools

import attr
from requests import Session as HTTPSession


# TODO AsyncRequest as AsyncContextManager vs as Coroutinge
# TODO AsyncHTTPSession should have a download() function instead of using predefined Download object in session

@attr.s()
class AsyncRequest(object):
    coro = attr.ib()

    def __await__(self):
        return self.coro.__await__()

    async def __aenter__(self):
        return await self.coro

    async def __aexit__(self, type, value, traceback):
        return


class AsyncHTTPSession(HTTPSession):
    def __init__(self, loop=None, executor=None, *args, **kwargs):
        self.loop = loop or asyncio.get_event_loop()
        self.executor = executor
        self.sync_request = super(AsyncHTTPSession, self).request
        super(AsyncHTTPSession, self).__init__(*args, **kwargs)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.close()

    def close(self):
        return self.loop.run_in_executor(self.executor, super(AsyncHTTPSession, self).close)

    def request(self, *args, **kwargs):
        return AsyncRequest(self.async_request(*args, **kwargs))

    async def async_request(self, *args, **kwargs):
        resp = await self.loop.run_in_executor(self.executor, functools.partial(self.sync_request, *args, **kwargs))
        resp.raise_for_status()
        return resp


class AsyncCachedHTTPSession(AsyncHTTPSession):
    def __init__(self, *args, **kwargs):
        super(AsyncCachedHTTPSession, self).__init__(*args, **kwargs)
        self.sync_request = functools.lru_cache()(self.sync_request)

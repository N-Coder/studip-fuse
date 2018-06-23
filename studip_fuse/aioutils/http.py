import asyncio
import functools

from requests import Session as HTTPSession


# TODO HTTP caching?
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

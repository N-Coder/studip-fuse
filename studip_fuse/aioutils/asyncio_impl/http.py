import asyncio
import functools
import logging
import re
from asyncio import Future

import attr
from requests import Session

from studip_fuse.aioutils.interface import Request, HTTPSession, Download

log = logging.getLogger(__name__)


# TODO AsyncRequest as AsyncContextManager vs as Coroutine
# TODO AsyncHTTPSession should have a download() function instead of using predefined Download object in session

@attr.s()
class AsyncioSyncRequestsRequest(Request):
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


class AsyncioSyncRequestsHTTPSession(HTTPSession):
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

    async def download(self, url, local_path):
        return AsyncioSyncRequestsDownload(url, local_path, self)

    async def async_request(self, *args, **kwargs):
        resp = await self.loop.run_in_executor(self.executor, functools.partial(self.sync_request, *args, **kwargs))
        resp.raise_for_status()
        return resp


class AsyncioSyncRequestsCachedHTTPSession(AsyncioSyncRequestsHTTPSession):
    def __init__(self, *args, **kwargs):
        super(AsyncioSyncRequestsCachedHTTPSession, self).__init__(*args, **kwargs)
        self.sync_request = functools.lru_cache()(self.sync_request)


@attr.s()
class AsyncioSyncRequestsDownload(Download):
    http = attr.ib()  # type: AsyncioSyncRequestsHTTPSession
    total_length = attr.ib(init=False, default=-1)  # type: int
    future = attr.ib(init=False, default=None)  # type: Future

    @property
    def is_running(self):
        return self.future is not None \
               and not self.future.done()

    @property
    def is_completed(self):
        return self.future is not None \
               and self.future.done() \
               and not self.future.exception() \
               and not self.future.cancelled()

    async def await_readable(self, offset=0, length=-1):
        if self.is_running:
            await self.future
        assert self.is_completed

    async def start(self):
        assert not self.is_running
        if not self.is_completed:
            self.future = self.http.loop.run_in_executor(self.http.executor, functools.partial(self.sync_start))
            await self.future

    def sync_start(self):
        with open(self.local_path, "wb", buffering=0) as f:
            with self.http.sync_request("GET", self.url, stream=True, allow_redirects=True) as r:
                r.raise_for_status()
                self.__extract_total_length(r)
                # self.fileio.truncate(self.total_length)

                while True:
                    chunk = r.iter_content(chunk_size=None, decode_unicode=False)
                    if not chunk:
                        break
                    f.write(chunk)

    def __extract_total_length(self, r):
        accept_ranges = r.headers.get("Accept-Ranges", "")
        if accept_ranges != "bytes":
            log.debug("Server is not indicating Accept-Ranges for file download:\n%s\n%s",
                      r.request_info, r)
        total_length = r.content_length or r.headers.get("Content-Length", None)
        if not total_length and "Content-Range" in r.headers:
            content_range = r.headers["Content-Range"]
            log.debug("Stud.IP didn't send Content-Length but Content-Range '%s'", content_range)
            match = re.match("bytes ([0-9]*)-([0-9]*)/([0-9]*)", content_range)
            log.debug("Extracted Content-Length from Content-Range: %s => %s", match,
                      match.groups() if match else "()")
            total_length = match.group(3)
        assert total_length, "Could not extract total file length from response %s" % repr(Download)
        self.total_length = int(total_length)

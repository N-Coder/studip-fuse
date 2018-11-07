import functools
import logging
import re
from datetime import datetime
from typing import Dict, Optional

import attr

from studip_fuse.aioutils.asyncio_impl.http import AsyncioSyncRequestsHTTPSession
from studip_fuse.aioutils.interface import Download, FileStore
from studip_fuse.avfs.path_util import join_path

log = logging.getLogger(__name__)


@attr.s()
class AsyncioFileStore(FileStore):
    http = attr.ib()  # type: AsyncioSyncRequestsHTTPSession
    location = attr.ib()  # type: str

    cache = attr.ib(init=False, default=attr.Factory(dict))  # type: Dict[str,Download]

    async def retrieve(self, uid: str, url: str, overwrite_created: Optional[datetime] = None, expected_size: Optional[int] = None) -> "Download":
        if uid in self.cache:
            return self.cache[uid]
        else:
            download = AsyncioSyncRequestsDownload(uid, url, join_path(self.location, uid), self.http)
            self.cache[uid] = download
            return download


@attr.s()
class AsyncioSyncRequestsDownload(Download):
    http = attr.ib()  # type: AsyncioSyncRequestsHTTPSession
    _total_length = attr.ib(init=False, default=-1)  # type: int
    future = attr.ib(init=False, default=None)  # type: Future

    @property
    def total_length(self):
        return self._total_length

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
        assert self.is_completed, "Download %s not done" % self # FIXME reraise cause?

    async def start(self):
        assert not self.is_running
        if not self.is_completed:
            self.future = self.http.loop.run_in_executor(self.http.executor, functools.partial(self.sync_start))

    def sync_start(self):
        with open(self.local_path, "wb", buffering=0) as f:
            with self.http.sync_request("GET", self.url, stream=True, allow_redirects=True) as r:
                r.raise_for_status()
                self.__extract_total_length(r)
                # self.fileio.truncate(self.total_length)

                for chunk in r.iter_content(chunk_size=None, decode_unicode=False):
                    if not chunk:
                        break
                    f.write(chunk)
                    # TODO re-add updates on_completed

    def __extract_total_length(self, r):
        accept_ranges = r.headers.get("Accept-Ranges", "")
        if accept_ranges != "bytes":
            log.debug("Server is not indicating Accept-Ranges for file download:\n%s\n%s",
                      r.request, r)
        total_length = getattr(r, "content_length", None) or r.headers.get("Content-Length", None)
        if not total_length and "Content-Range" in r.headers:
            content_range = r.headers["Content-Range"]
            log.debug("Stud.IP didn't send Content-Length but Content-Range '%s'", content_range)
            match = re.match("bytes ([0-9]*)-([0-9]*)/([0-9]*)", content_range)
            log.debug("Extracted Content-Length from Content-Range: %s => %s", match,
                      match.groups() if match else "()")
            total_length = match.group(3)
        assert total_length, "Could not extract total file length from response %s" % repr(Download)
        self._total_length = int(total_length)

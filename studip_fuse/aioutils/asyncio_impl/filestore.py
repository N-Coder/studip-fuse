from datetime import datetime
from typing import Optional

import attr

from studip_fuse.aioutils.interface import Download, FileStore


@attr.s()
class AsyncioFileStore(FileStore):
    http = attr.ib()  # type: AsyncioSyncRequestsHTTPSession
    location = attr.ib()

    async def retrieve(self, uid: str, url: str, overwrite_created: Optional[datetime] = None, expected_size: Optional[int] = None) -> "Download":
        pass


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

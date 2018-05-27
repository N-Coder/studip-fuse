import asyncio
import logging
import os
import re
from typing import Callable, Coroutine, List, Tuple, Union

import aiofiles
import aiohttp
import attr
import more_itertools
from aiofiles.threadpool import AsyncFileIO
from attr import Factory
from cached_property import cached_property

log = logging.getLogger(__name__)
log_downloading = log.getChild("progress")


@attr.s()
class Download(object):
    ahttp = attr.ib()  # type: aiohttp.ClientSession
    url = attr.ib()  # type: str
    local_path = attr.ib()  # type: str
    chunk_size = attr.ib(default=1024 * 256)  # type: int

    total_length = attr.ib(init=False, default=-1)  # type: int
    aiofile = attr.ib(init=False, default=None)  # type: AsyncFileIO
    parts = attr.ib(init=False, default=None)  # type: List[Tuple[range, asyncio.Future[range]]]
    completed = attr.ib(init=False, default=None)  # type: asyncio.Future[List[range]]
    on_completed = attr.ib(default=Factory(list))  # type: List[Callable[[Download, Union[List[range], Exception]],Coroutine[None]]]

    @cached_property
    def write_lock(self) -> asyncio.Lock:
        # initialize lazy, so that asyncio.get_event_loop() doesn't create a new event loop before the actual one is set
        return asyncio.Lock()

    # noinspection PyProtectedMember
    @property
    def oiofile(self):
        return self.aiofile._file

    @property
    def fileno(self):
        return self.oiofile.fileno()

    # noinspection PyProtectedMember
    @property
    def loop(self) -> asyncio.BaseEventLoop:
        return self.aiofile._loop

    # noinspection PyProtectedMember
    @property
    def executor(self):
        return self.aiofile._executor

    async def load_completed(self):
        self.total_length = await self.fetch_total_length()
        async with aiofiles.open(self.local_path, "rb", buffering=0) as self.aiofile:
            async with self.write_lock:
                old_file_position = await self.aiofile.tell()
                await self.aiofile.seek(0, os.SEEK_END)
                file_length = await self.aiofile.tell()
                assert file_length == self.total_length, \
                    "Was told to load Stud.IP file with size %s from file with size %s" % \
                    (self.total_length, file_length)
                await self.aiofile.seek(old_file_position, os.SEEK_SET)

            full_range = range(0, self.total_length)
            full_range_future = self.loop.create_future()
            full_range_future.set_result(full_range)
            self.parts = [(full_range, full_range_future)]
            self.completed = self.loop.create_future()
            try:
                await self.__on_completed(full_range)
                self.completed.set_result(full_range)
            except Exception as e:
                self.completed.set_exception(e)
                raise

        log.debug("Loaded completed download %s containing %s bytes", self.local_path, self.total_length)

    async def __await_completed(self):
        try:
            completed_ranges = await asyncio.gather(*(f for r, f in self.parts))
            log.debug("Finished download of %s, expecting %s bytes split into %s parts",
                      self.local_path, self.total_length, len(self.parts))
            await self.__on_completed(completed_ranges)
            return completed_ranges
        except Exception as e:
            # XXX if the exception originated from a callback, this will call all the callbacks again
            await self.__on_completed(e)
            raise
        finally:
            await self.aiofile.close()

    async def __on_completed(self, result):
        for cb in self.on_completed:
            try:
                await cb(self, result)
            except Exception as e:
                log.warning("Download completed callback %s raised exception %s, marking Download %s as failed.",
                            cb, e, self, exc_info=True)
                raise

    async def start(self):
        self.total_length = await self.fetch_total_length()

        self.aiofile = await aiofiles.open(self.local_path, "wb", buffering=0)
        try:
            await self.aiofile.truncate(self.total_length)
            ranges = list(more_itertools.sliced(range(self.total_length), self.chunk_size))
            # FIXME this starts a lot of parallel downloads, which will timeout waiting for the few connections from the pool
            # FIXME individual parts can't be retried, but will fail the whole download (maybe use circuit breaker / async cache?)
            self.parts = [(r, asyncio.ensure_future(self.download_range(r))) for r in ranges]
            log.debug("Started download of %s, expecting %s bytes split into %s parts",
                      self.local_path, self.total_length, len(self.parts))
        except:
            self.aiofile.close()
            raise
        self.completed = asyncio.ensure_future(self.__await_completed())

    async def fork(self):
        fork = Download(self.ahttp, self.url, self.local_path, self.chunk_size)
        assert self.total_length >= 0, "tried to fork Download that wasn't started"
        fork.total_length = self.total_length
        fork.aiofile = await aiofiles.open(self.local_path, "wb", buffering=0)
        fork.on_completed.extend(self.on_completed)
        retried = kept = 0

        def retry_range(rnge, future):
            nonlocal retried, kept
            if future.cancelled() or future.exception():
                retried += 1
                return asyncio.ensure_future(fork.download_range(rnge))
            else:
                kept += 1
                return future

        try:
            fork.parts = [(r, retry_range(r, f)) for r, f in self.parts]
            log.debug("Forked download of %s, expecting %s bytes split into %s parts "
                      "(of which %s were kept and %s will be retried)",
                      fork.local_path, fork.total_length, len(fork.parts), kept, retried)
        except:
            fork.aiofile.close()
            raise

        fork.completed = asyncio.ensure_future(self.__await_completed())
        return fork

    async def fetch_total_length(self):
        async with self.ahttp.head(self.url) as r:
            r.raise_for_status()
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
            total_length = int(total_length)
        return total_length

    async def download_range(self, byte_range):
        async with self.ahttp.get(self.url, headers={
            "Range": "bytes={0}-{1}".format(byte_range.start, byte_range.stop)
        }) as resp:
            resp.raise_for_status()
            actual_range = self._extract_range(resp, byte_range)

            offset = byte_range.start
            while True:
                chunk, end_of_HTTP_chunk = await resp.content.readchunk()
                if not chunk:
                    break
                log_downloading.debug("Chunk %s: writing at offset %6d + %6d new bytes = %6d new offset. Data: %s...%s",
                                      actual_range, offset, len(chunk), offset + len(chunk), chunk[:10], chunk[-10:])
                written = await self._write_chunk(chunk, offset)
                offset += written

        await self.aiofile.flush()
        log_downloading.debug("Chunk %s: wrote bytes from %6d to %6d", actual_range, byte_range.start, offset)
        return range(byte_range.start, offset)

    def _extract_range(self, resp, expected_byte_range):
        requested_rage = resp.request_info.headers.get("Range", "")
        expected_range = "bytes %s-%s/%s" % \
                         (expected_byte_range.start, expected_byte_range.stop - 1, self.total_length)
        expected_range_plus1 = "bytes %s-%s/%s" % \
                               (expected_byte_range.start, expected_byte_range.stop, self.total_length)
        actual_range = resp.headers.get("Content-Range", "")
        if expected_range != actual_range and expected_range_plus1 != actual_range:
            log.warning("Requested range %s, expected %s, got %s",
                        requested_rage, expected_range, actual_range)
        return actual_range

    async def _write_chunk(self, chunk, offset):
        async with self.write_lock:
            return await self.loop.run_in_executor(
                self.executor,
                self._blocking_write_chunk, chunk, offset)

    def _blocking_write_chunk(self, chunk, offset):
        log_downloading.debug("FH %s: writing at offset %6d + %6d new bytes = %6d new offset. Data: %s...%s",
                              self.oiofile, offset, len(chunk), offset + len(chunk), chunk[:10], chunk[-10:])

        # once the file handle is closed all pending operations should be cancelled
        if self.oiofile.closed:
            assert self.completed.done(), "File %s was closed before completion future %s was done." \
                                          % (self.oiofile, self.completed)
            raise asyncio.CancelledError() from self.completed.exception()

        pos = self.oiofile.seek(offset)
        written = self.oiofile.write(chunk)
        new_offset = self.oiofile.tell()

        log_downloading.debug("FH %s: wrote   at offset %6d + %6d new bytes = %6d new offset",
                              self.oiofile, pos, written, new_offset)

        assert pos == offset, "Tried to seek to %s, but position is %s" % (offset, pos)
        assert written == len(chunk), "Tried to write chunk of size %s, but only wrote %s" % (len(chunk), written)
        assert new_offset == offset + written, "File should be at position %s after writing, but is at %s" % \
                                               (offset + written, new_offset)

        return written

    async def await_readable(self, offset, length):
        if self.completed.done():
            # Rethrow exception if one of the ranges failed. This may lead to a range first being readable,
            # but becoming unreadable later if any other range fails after this one was completed.
            self.completed.result()
            return

        requested_range = range(offset, min(offset + length, self.total_length))
        completed_ranges = []
        for r, f in self.parts:
            if max(requested_range.start, r.start) < min(requested_range.stop, r.stop):
                completed_ranges.append(await f)

        assert len(completed_ranges) > 0, \
            "No range of file (length %s) seems to satisfy read request with offset %s and length %s." % \
            (self.total_length, offset, length)
        first = completed_ranges[0].start
        last = first
        for r in completed_ranges:
            assert r.start <= last, "Non-connected ranges: %s" % completed_ranges
            last = r.stop
        assert first <= requested_range.start, "Completed range(%s, %s) doesn't cover requested %s" % \
                                               (first, last, requested_range)
        assert last >= requested_range.stop, "Completed range(%s, %s) doesn't cover requested %s" % \
                                             (first, last, requested_range)

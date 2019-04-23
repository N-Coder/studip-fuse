import asyncio
import enum
import logging
import os
import re
import time
import warnings
from asyncio import CancelledError, Future
from datetime import datetime
from stat import S_ISREG
from typing import Callable, Union

import aiofiles
import aiofiles.os
import aiohttp
import attr
from aiohttp import ClientRequest, hdrs, helpers
from async_exit_stack import AsyncExitStack
from async_generator import async_generator, asynccontextmanager, yield_
from async_lru import alru_cache
from oauthlib.oauth1 import Client as OAuth1Client
from pyrsistent import freeze
from yarl import URL

from studip_fuse.studipfs.api.aiobase import BaseHTTPClient
from studip_fuse.studipfs.api.aiointerface import Download

log = logging.getLogger(__name__)

async_stat = aiofiles.os.stat
async_utime = aiofiles.os.wrap(os.utime)


class AuthenticatedClientRequest(ClientRequest):
    def update_auth(self, auth):
        if auth is None:
            auth = self.auth
        if auth is None:
            return

        if isinstance(auth, helpers.BasicAuth):
            self.headers[hdrs.AUTHORIZATION] = auth.encode()
        elif isinstance(auth, OAuth1Client):
            url, headers, _ = auth.sign(
                str(self.url), str(self.method), None, self.headers
            )
            self.url = URL(url)
            self.update_headers(headers)
        elif callable(auth):
            auth(self)
        else:
            raise TypeError('auth should be a BasicAuth() tuple, OAuth1Client or Callable')


@attr.s()
class AiohttpClient(BaseHTTPClient):
    http_session = attr.ib()  # type: Union[aiohttp.ClientSession, Callable[[], aiohttp.ClientSession]]
    exit_stack = attr.ib(init=False, default=attr.Factory(AsyncExitStack))  # type: AsyncExitStack

    @property
    def loop(self):
        return self.http_session.loop

    async def __aenter__(self):
        if callable(self.http_session):
            self.http_session = self.http_session()
        self.http_session = await self.exit_stack.enter_async_context(self.http_session)
        self.get_json = alru_cache(self.get_json, loop=self.loop, cache_exceptions=False)
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.exit_stack.aclose()

    async def get_json(self, url):
        async with self.http_session.get(url) as resp:
            resp.raise_for_status()
            return freeze(await resp.json())

    async def get_text(self, url):
        async with self.http_session.get(url) as resp:
            resp.raise_for_status()
            return await resp.text()

    async def retrieve_missing(self, uid, url, overwrite_created, expected_size):
        download = AiohttpDownload(uid, url, self.uid_to_path(uid), expected_size, overwrite_created, self)
        self.exit_stack.push_async_exit(download.aclose)
        return download

    async def basic_auth(self, username, password):
        self.http_session._default_auth = aiohttp.BasicAuth(username, password)

    async def oauth1_auth(self, **kwargs):
        self.http_session._default_auth = OAuth1Client(**kwargs)

    async def shib_auth(self, start_url, username, password):
        async with self.http_session.get(start_url) as resp:
            resp.raise_for_status()
            post_url = self.parse_login_form(await resp.text())
            post_url = URL(resp.url).join(URL(post_url))

        async with self.http_session.post(
                post_url,
                data={
                    "j_username": username,
                    "j_password": password,
                    "uApprove.consent-revocation": "",
                    "_eventId_proceed": "",
                    "donotcache": "",
                    "_shib_idp_revokeConsent": "false"
                }) as resp:
            resp.raise_for_status()
            form_url, form_data = self.parse_saml_form(await resp.text())
            form_url = URL(resp.url).join(URL(form_url))

        async with self.http_session.post(form_url, data=form_data) as resp:
            resp.raise_for_status()


class DownloadState(enum.Enum):
    EMPTY = 1

    VALIDATING = 2
    LOADING = 3

    DONE = 4
    FAILED = 5


@attr.s()
class AiohttpDownload(Download):
    http_client = attr.ib()  # type: AiohttpClient

    state = attr.ib(init=False, default=DownloadState.EMPTY)  # type: DownloadState
    future = attr.ib(init=False, default=None)  # type: Future

    @property
    def http_session(self):
        return self.http_client.http_session

    @property
    def loop(self):
        return self.http_client.loop

    @property
    def is_loading(self) -> bool:
        if self.state == DownloadState.VALIDATING:
            return True
        elif self.state == DownloadState.LOADING:
            assert self.future is not None and not self.future.done()
            return True
        else:
            assert self.state in [DownloadState.EMPTY, DownloadState.DONE, DownloadState.FAILED]
            assert self.future is None or self.future.done()
            return False

    @property
    def is_completed(self) -> bool:
        if self.state == DownloadState.DONE:
            assert not self.exception()
            return True
        else:
            return False

    def exception(self):
        if self.future and self.future.done():
            try:
                exc = self.future.exception()
            except CancelledError as e:
                exc = e
            if exc:
                assert self.state == DownloadState.FAILED
            else:
                assert self.state == DownloadState.DONE
            return exc

    async def aclose(self, exc_type=None, exc_val=None, exc_tb=None):
        if self.future:
            if self.future.done() and self.future.exception():
                # don't await a failed future multiple times, this will mess up the stack trace
                raise RuntimeError("Background download failed: %s" % self.future) from self.future.exception()
            await self.future
        assert not self.is_loading

    @asynccontextmanager
    @async_generator
    async def state_manager(self):
        assert not self.is_loading and not self.is_completed
        self.state = DownloadState.VALIDATING
        try:
            await yield_()
        finally:
            if self.state != DownloadState.DONE:
                self.state = DownloadState.FAILED

    async def run_in_background(self, stack, aiofile, resp):
        async with stack:
            self.state = DownloadState.LOADING
            async for data in resp.content.iter_any():
                await aiofile.write(data)
            position = await aiofile.tell()
            assert position == self.total_length, "Download %s only got %s of %s bytes" % (self, position, self.total_length)
            timestamp = time.mktime(self.last_modified.timetuple())
            if os.utime in os.supports_fd:
                await async_utime(aiofile.fileno(), (timestamp, timestamp))
            else:
                await aiofile.close()
                await async_utime(self.local_path, (timestamp, timestamp))  # FIXME mtime is not stable on Windows
            self.state = DownloadState.DONE

    async def start_loading(self):
        if self.is_loading:
            return
        if self.is_completed:
            assert await self.is_cached_locally(), "Cache file %s is gone." % self.local_path
            return

        await self.aclose()  # ensure old Future was properly awaited first
        async with AsyncExitStack() as stack:
            await stack.enter_async_context(self.state_manager())

            if await self.is_cached_locally():
                self.state = DownloadState.DONE
                return
            await self.validate_headers()

            aiofile = await stack.enter_async_context(aiofiles.open(self.local_path, "wb", buffering=0))
            await aiofile.truncate(self.total_length)
            await aiofile.seek(0, os.SEEK_SET)

            resp = await stack.enter_async_context(self.http_session.get(self.url, chunked=True))
            resp.raise_for_status()

            self.future = asyncio.ensure_future(
                self.run_in_background(stack.pop_all(), aiofile, resp),
                # After stack.pop_all(), the former exit stack will be contained in the self.future/run_in_background() 'closure'
                # and reliably cleaned up by self.aclose() right after the background Task completed.
                loop=self.loop)

    async def await_readable(self, offset=0, length=-1):
        await self.aclose()  # TODO would be sufficient to only wait for requested range

    async def is_cached_locally(self):
        try:
            stat = await async_stat(self.local_path)
            assert S_ISREG(stat.st_mode), \
                "Was told to load Stud.IP file from irregular local file %s (%s)" % (self.local_path, stat)
            if self.total_length:
                assert self.total_length == stat.st_size, \
                    "Was told to load Stud.IP file with size %s from local file %s with size %s" % \
                    (self.total_length, self.local_path, stat.st_size)
            if self.last_modified:
                st_mtime = datetime.fromtimestamp(stat.st_mtime)
                assert self.last_modified == st_mtime, \
                    "Was told to load Stud.IP file with last change %s from local file %s with last change %s" % \
                    (self.last_modified, self.local_path, st_mtime)
            return True
        except FileNotFoundError:
            return False

    async def validate_headers(self):
        async with self.http_session.head(self.url) as r:
            if r.status == 405:
                warnings.warn("Server doesn't allow HEAD requests for Download URLs.")
                return
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
            assert self.total_length == int(total_length), \
                "Was told to load Stud.IP file with size %s from HTTP download reporting size %s" % \
                (self.total_length, total_length)

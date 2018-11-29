import asyncio
import enum
import logging
import os
import re
from asyncio import Future
from contextlib import AsyncExitStack
from datetime import datetime
from stat import S_ISREG
from typing import Callable, Dict, Optional, Union

import aiofiles
import aiohttp
import attr
from async_lru import alru_cache
from bs4 import BeautifulSoup
from pyrsistent import freeze

from studip_fuse.studipfs.api.aiointerface import Download, HTTPClient, HTTPResponse

log = logging.getLogger(__name__)


def parse_login_form(html):
    with open("/tmp/login_form.html", "w") as f:
        f.write(html)
    soup = BeautifulSoup(html)
    for form in soup.find_all('form'):
        if 'action' in form.attrs:
            return form.attrs['action']
    raise PermissionError("Could not find login form", soup)


def parse_saml_form(html):
    with open("/tmp/saml_form.html", "w") as f:
        f.write(html)
    soup = BeautifulSoup(html)
    saml_fields = {'RelayState', 'SAMLResponse'}
    form_data = {}
    form_url = None
    p = soup.find('p')
    if 'class' in p.attrs and 'form-error' in p.attrs['class']:
        raise PermissionError("Error in Request: '%s'" % p.text, soup)
    for input in soup.find_all('input'):
        if 'name' in input.attrs and 'value' in input.attrs and input.attrs['name'] in saml_fields:
            form_data[input.attrs['name']] = input.attrs['value']

    return form_url, form_data


@attr.s()
class AiohttpClient(HTTPClient):
    http_session = attr.ib()  # type: Union[aiohttp.ClientSession, Callable[[],aiohttp.ClientSession]]
    storage_dir = attr.ib()  # type: str

    download_cache = attr.ib(init=False, default=attr.Factory(dict))  # type: Dict[str, "AiohttpDownload"]
    exit_stack = attr.ib(init=False, default=attr.Factory(AsyncExitStack))  # type: AsyncExitStack

    @property
    def loop(self):
        return self.http_session.loop

    async def __aenter__(self):
        if callable(self.http_session):
            self.http_session = self.http_session()
        self.http_session = await self.exit_stack.enter_async_context(self.http_session)
        self.get_json = alru_cache(self.get_json, loop=self.loop)
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.exit_stack.aclose()

    async def get_json(self, url):
        async with self.http_session.get(url) as resp:
            resp.raise_for_status()
            return freeze(await resp.json())

    def uid_to_path(self, uid):
        return os.path.join(self.storage_dir, uid)

    async def retrieve(self, uid: str, url: str, overwrite_created: Optional[datetime] = None, expected_size: Optional[int] = None) -> "AiohttpDownload":
        if uid in self.download_cache:
            download = self.download_cache[uid]
            assert download.url == url
            assert download.total_length is None or download.total_length == expected_size
            assert download.last_modified is None or download.last_modified == overwrite_created
            return download
        else:
            download = AiohttpDownload(uid, url, self.uid_to_path(uid), expected_size, overwrite_created, self)
            self.exit_stack.push_async_exit(download.aclose)
            self.download_cache[uid] = download
            return download

    async def basic_auth(self, url, username, password) -> HTTPResponse:
        self.http_session._default_auth = aiohttp.BasicAuth(username, password)
        async with self.http_session.get(url) as resp:
            resp.raise_for_status()
            try:
                return HTTPResponse(resp.url, resp.headers, await resp.json())
            except aiohttp.ContentTypeError:
                return HTTPResponse(resp.url, resp.headers, await resp.text())

    async def oauth2_auth(self, *args) -> HTTPResponse:
        raise NotImplementedError()

    async def shib_auth(self, url, username, password) -> HTTPResponse:
        if not url:
            url = "/studip/index.php?again=yes&sso=shib"

        async with self.http_session.get(url) as resp:
            resp.raise_for_status()
            post_url = parse_login_form(await resp.text())

        async with self.http_session.post(
                post_url,
                data={
                    "j_username": username,
                    "j_password": password,
                    "uApprove.consent-revocation": "",
                    "_eventId_proceed": ""
                }) as resp:
            resp.raise_for_status()
            form_url, form_data = parse_saml_form(await resp.text())

        async with self.http_session.post(form_url, data=form_data) as resp:
            resp.raise_for_status()
            # TODO check that were redirected to Stud.IP and aren't trapped in Shib (due to expired/wrong password etc)
            try:
                return HTTPResponse(resp.url, resp.headers, await resp.json())
            except aiohttp.ContentTypeError:
                return HTTPResponse(resp.url, resp.headers, await resp.text())


class DownloadState(enum.Enum):
    EMPTY = enum.auto()

    VALIDATING = enum.auto()
    LOADING = enum.auto()

    DONE = enum.auto()
    FAILED = enum.auto()


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
            assert self.future is None or self.future.done()
            return False

    @property
    def is_completed(self) -> bool:
        return self.state == DownloadState.DONE

    async def aclose(self):
        pass

    async def start_loading(self):
        if not self.is_loading and not self.is_completed:
            self.state = DownloadState.VALIDATING
            if await self.is_cached_locally():
                self.state = DownloadState.DONE
                return
            # await self.validate_headers()

            async with AsyncExitStack() as stack:
                later_stack = stack

                async def finalize(extype, exvalue, extraceback):
                    assert self.is_loading
                    if extype:
                        self.state = DownloadState.FAILED
                    else:
                        self.state = DownloadState.DONE
                    return False  # propagate

                stack.push(finalize)

                aiofile = await stack.enter_async_context(aiofiles.open(self.local_path, "wb", buffering=0))
                await aiofile.truncate(self.total_length)
                await aiofile.seek(0, os.SEEK_SET)

                resp = await stack.enter_async_context(self.http_session.get(self.url, chunked=True))
                resp.raise_for_status()

                async def run():
                    # FIXME awaiting this doesnt work
                    async with later_stack:
                        self.state = DownloadState.LOADING
                        async for data in resp.content.iter_any():
                            await aiofile.write(data)

                self.future = asyncio.ensure_future(run(), loop=self.loop)
                later_stack = stack.pop_all()  # don't call aexit now, but later

    async def await_readable(self, offset=0, length=-1):
        if self.future:
            await self.future

    async def is_cached_locally(self):
        try:
            stat = await self.loop.run_in_executor(None, os.stat, self.local_path)
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

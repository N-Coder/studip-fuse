import asyncio
import concurrent.futures
import errno
import logging.handlers
import os
import signal
import tempfile
from asyncio import BaseEventLoop
from stat import S_IFREG
from threading import Thread
from typing import Dict, List

import attr
from attr import Factory
from fuse import LoggingMixIn, Operations, fuse_get_context

from studip_api.downloader import Download
from studip_fuse.__main__.main_loop import main_loop
from studip_fuse.__main__.thread_util import await_loop_thread_shutdown
from studip_fuse.cache import cached_task
from studip_fuse.cache.async_cache import clear_caches
from studip_fuse.path import RealPath, VirtualPath, path_name

log = logging.getLogger("studip_fuse.fs_driver")


# https://www.cs.hmc.edu/~geoff/classes/hmc.cs135.201001/homework/fuse/fuse_doc.html
@attr.s(hash=False)
class FUSEView(Operations):
    args = attr.ib()
    http_args = attr.ib()
    fuse_args = attr.ib()

    loop_future = attr.ib(init=False, default=None)
    loop_thread = attr.ib(init=False, default=None)
    loop = attr.ib(init=False, default=None)  # type: BaseEventLoop
    session = attr.ib(init=False, default=None)
    root_rp = attr.ib(init=False, default=None)  # type: RealPath
    open_files = attr.ib(init=False, default=Factory(dict))  # type: Dict[str, Download]

    def init(self, path):
        try:
            log.info("Mounting at %s (uid=%s, gid=%s, pid=%s, python pid=%s)", path, *fuse_get_context(),
                     os.getpid())

            self.loop_future = concurrent.futures.Future()
            self.loop_thread = Thread(target=main_loop, args=(self.args, self.http_args, self.loop_future),
                                      name="aio event loop", daemon=True)
            self.loop_thread.start()
            log.debug("Event loop thread started, waiting for session initialization")
            self.loop, self.session = self.loop_future.result()

            vp = VirtualPath(session=self.session, path_segments=[], known_data={}, parent=None,
                             next_path_segments=self.args.format.split("/"))
            self.root_rp = RealPath(parent=None, generating_vps={vp})
            log.debug("Session and virtual FS initialized")

            log.info("Mounting complete")
        except:
            # the raised exception (even SystemExit) would be caught by FUSE, so tell system to interrupt FUSE
            os.kill(os.getpid(), signal.SIGINT)
            raise

    def destroy(self, path):
        log.info("Unmounting from %s (uid=%s, gid=%s, pid=%s, python pid=%s)", path, *fuse_get_context(),
                 os.getpid())

        if self.loop_future:
            self.loop_future.cancel()
        if self.loop:
            self.loop.stop()
        if self.loop_thread:
            await_loop_thread_shutdown(self.loop, self.loop_thread)

        log.info("Unmounting complete")

    def schedule_async(self, coro):
        if not self.loop:
            raise RuntimeError("Can't await async operation while event loop isn't available")
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def _resolve(self, partial: str) -> RealPath:
        coro = self._aresolve(partial)
        task = self.schedule_async(coro)
        return task.result()

    @cached_task()
    async def _aresolve(self, path: str) -> RealPath:
        resolved_real_file = await self.root_rp.resolve(path)
        if not resolved_real_file:
            raise OSError(errno.ENOENT, path)
        else:
            return resolved_real_file

    def readdir(self, path, fh) -> List[str]:
        return self.schedule_async(self._areaddir(path)).result()

    @cached_task()
    async def _areaddir(self, path) -> List[str]:
        resolved_real_file = await self.root_rp.resolve(path)
        if not resolved_real_file:
            raise OSError(errno.ENOENT, path)
        elif resolved_real_file.is_folder:
            return ['.', '..'] + [path_name(rp.path) for rp in await resolved_real_file.list_contents()]
        else:
            raise OSError(errno.ENOTDIR)

    def access(self, path, mode):
        if path == "/.clear_caches":
            return
        return self._resolve(path).access(mode)

    def getattr(self, path, fh=None):
        if path == "/.clear_caches":
            return dict(st_mode=(S_IFREG | 0o755), st_nlink=1, st_size=2048)
        return self._resolve(path).getattr()

    def open(self, path, flags):
        if path == "/.clear_caches":
            msg = self.schedule_async(clear_caches()).result()
            fh, tmp_path = tempfile.mkstemp()
            os.write(fh, msg.encode())
            return fh

        resolved_real_file = self._resolve(path)
        if resolved_real_file.is_folder:
            raise OSError(errno.EISDIR)
        else:
            download = self.schedule_async(resolved_real_file.open_file(flags)).result()
            fileno = os.open(download.local_path, flags)
            self.open_files[fileno] = download
            return fileno

    def read(self, path, length, offset, fh):
        download = self.open_files.get(fh, None)
        if download:
            self.schedule_async(download.await_readable(offset, length)).result()

        os.lseek(fh, offset, os.SEEK_SET)
        return os.read(fh, length)

    def flush(self, path, fh):
        return os.fsync(fh)

    def release(self, path, fh):
        self.open_files.pop(fh, None)
        return os.close(fh)

    def fsync(self, path, fdatasync, fh):
        return self.flush(path, fh)


class LoggingFUSEView(FUSEView, LoggingMixIn):
    pass

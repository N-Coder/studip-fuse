import asyncio
import concurrent.futures
import errno
import inspect
import logging.handlers
import os
import pprint
from asyncio import BaseEventLoop
from threading import Lock, Thread
from typing import Dict, List

import attr
from attr import Factory
from fuse import FuseOSError, fuse_get_context
from more_itertools import one

from studip_fuse.avfs.path_util import path_name
from studip_fuse.avfs.real_path import RealPath
from studip_fuse.studipfs.main_loop.start import setup_loop
from studip_fuse.studipfs.main_loop.stop import ThreadSafeDefaultDict, await_loop_thread_shutdown
from studip_fuse.studipfs.path.studip_path import StudIPPath

ENOATTR = getattr(errno, "ENOATTR", getattr(errno, "ENODATA"))

log = logging.getLogger(__name__)
log_ops = log.getChild("ops")


def log_status(status, args=None, level=logging.INFO):
    args = (status, *fuse_get_context(), os.getpid(), args.user if args else "?", args.mount if args else "?")
    logging.getLogger("studip_fuse.status").log(level, " ".join(["%s"] * len(args)), *args)


@attr.s(hash=False)
class FUSEView(object):
    args = attr.ib()
    http_args = attr.ib()
    fuse_args = attr.ib()

    loop_future = attr.ib(init=False, default=None)
    loop_thread = attr.ib(init=False, default=None)
    api_thread = attr.ib(init=False, default=None)
    loop = attr.ib(init=False, default=None)  # type: BaseEventLoop
    session = attr.ib(init=False, default=None)  # type: CachedStudIPSession
    root_rp = attr.ib(init=False, default=None)  # type: RealPath
    open_files = attr.ib(init=False, default=Factory(dict))  # type: Dict[str, Download]

    @staticmethod
    def saferepr(val):
        val = pprint.saferepr(val)
        if len(val) > 2000:
            val = val[:1985] + "[...]" + val[-10:]
        return val

    def __call__(self, op, path, *args):
        if log_ops.isEnabledFor(logging.DEBUG):
            signature = inspect.signature(getattr(self, op))
            bound_args = signature.bind(path, *args)
            bound_args.apply_defaults()
            log_ops.debug('-> %s %s %s', op, path, self.saferepr(bound_args.arguments))

        ret = '[Unhandled Exception]'
        try:
            if not hasattr(self, op):
                raise FuseOSError(errno.ENOSYS)
            ret = getattr(self, op)(path, *args)
            return ret
        except OSError as e:
            ret = str(e)
            raise
        finally:
            if log_ops.isEnabledFor(logging.DEBUG):
                log_ops.debug('<- %s %s', op, self.saferepr(ret))

    def init(self, path):
        log_status("INITIALIZING", args=self.args)
        log.info("Mounting at %s (uid=%s, gid=%s, pid=%s, python pid=%s)", path, *fuse_get_context(),
                 os.getpid())

        self.loop_future = concurrent.futures.Future()
        self.loop_thread = Thread(target=setup_loop, args=(self.args, self.http_args, self.loop_future),
                                  name="aio event loop", daemon=True)
        self.loop_thread.start()
        log.debug("Event loop thread started, waiting for session initialization")
        self.loop, self.session = self.loop_future.result()

        vp = StudIPPath(session=self.session, path_segments=[], known_data={}, parent=None,
                        next_path_segments=self.args.format.split("/"))
        self.root_rp = RealPath(parent=None, generating_vps={vp})
        log.debug("Session and virtual FS initialized")

        # from studip_fuse.__main__.http_api import run
        # self.api_thread = Thread(target=run, args=(self,), name="HTTP server thread", daemon=True)
        # self.api_thread.start()
        # log.debug("HTTP API running")

        log_status("READY", args=self.args)
        log.info("Mounting complete")

    def destroy(self, path):
        log_status("STOPPING", args=self.args)
        log.info("Unmounting from %s (uid=%s, gid=%s, pid=%s, python pid=%s)", path, *fuse_get_context(),
                 os.getpid())

        if self.loop_future:
            self.loop_future.cancel()
        if self.loop and self.loop.is_running():
            self.loop.call_soon_threadsafe(self.loop.stop)
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

    async def _aresolve(self, path: str) -> RealPath:
        resolved_real_file = await self.root_rp.resolve(path)
        if not resolved_real_file:
            return -errno.ENOENT
        else:
            return resolved_real_file

    def readdir(self, path, fh) -> List[str]:
        return self.schedule_async(self._areaddir(path)).result()

    async def _areaddir(self, path) -> List[str]:
        resolved_real_file = await self.root_rp.resolve(path)
        if not resolved_real_file:
            return -errno.ENOENT
        elif resolved_real_file.is_folder:
            return ['.', '..'] + [path_name(rp.path) for rp in await resolved_real_file.list_contents()]
        else:
            return -errno.ENOTDIR

    def access(self, path, mode):
        # FIXME all methods are async now
        return self._resolve(path).access(mode)

    def getattr(self, path, fh=None):
        return self.schedule_async(self._resolve(path).getattr()).result()

    def open(self, path, flags):
        resolved_real_file = self._resolve(path)
        if resolved_real_file.is_folder:
            return -errno.EISDIR
        else:
            download = self.schedule_async(resolved_real_file.open_file(flags)).result()
            if os.name == 'nt' and not flags & getattr(os, "O_TEXT", 16384):
                flags |= os.O_BINARY
            fileno = os.open(download.local_path, flags)
            self.open_files[fileno] = download
            return fileno

    read_locks = attr.ib(init=False, repr=False, default=Factory(lambda: ThreadSafeDefaultDict(Lock)))

    def read(self, path, length, offset, fh):
        download = self.open_files.get(fh, None)
        if download:
            self.schedule_async(download.await_readable(offset, length)).result()

        with self.read_locks[fh]:
            os.lseek(fh, offset, os.SEEK_SET)
            return os.read(fh, length)

    def flush(self, path, fh):
        return os.fsync(fh)

    def release(self, path, fh):
        self.open_files.pop(fh, None)
        return os.close(fh)

    def fsync(self, path, fdatasync, fh):
        return self.flush(path, fh)

    def get_content_task(self, path) -> asyncio.Future:
        realpath = self._resolve(path)
        if realpath.is_folder:
            return realpath.list_contents(_AsyncCache__only_cached=True)

        else:
            file = one(realpath.generating_vps)._file
            if self.schedule_async(self.session.has_cached_download(file)).result():
                # the file was already loaded, so it's save to force cache value creation without triggering a download
                download = self.schedule_async(realpath.open_file(os.O_RDONLY)).result()
                return download.completed
            else:
                download_future = self.session.download_file_contents(file, _AsyncCache__only_cached=True)
                if isinstance(download_future, asyncio.Future) and download_future.done() \
                        and not download_future.cancelled() and not download_future.exception():
                    # if the download was already started, return the download.completed future tracking download progress
                    download = download_future.result()
                    return download.completed
                else:
                    return download_future

    def getxattr(self, path, name, position=0):
        if name == "user.studip-fuse.contents-status":
            coro = self.get_content_task(path)
            if not isinstance(coro, asyncio.Future):
                return "unknown".encode()  # == "unavailable-offline"
            elif not coro.done():
                return "pending".encode()
            elif coro.cancelled() or coro.exception():
                return "failed".encode()
            else:
                return "available".encode()
        elif name == "user.studip-fuse.contents-exception":
            coro = self.get_content_task(path)
            if not isinstance(coro, asyncio.Future):
                return "InvalidStateError: operation was not started yet".encode()
            elif not coro.done():
                return "InvalidStateError: operation is not complete yet".encode()
            elif coro.cancelled():
                return "CancelledError: operation was cancelled".encode()
            elif coro.exception():
                return ("%s: %s" % (coro.exception().__class__.__name__, coro.exception())).encode()
            else:
                return "".encode()
        else:
            return -ENOATTR

    def listxattr(self, path):
        return ["user.studip-fuse.contents-status", "user.studip-fuse.contents-exception"]

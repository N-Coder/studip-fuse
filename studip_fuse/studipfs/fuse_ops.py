import asyncio
import concurrent.futures
import errno
import functools
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

from studip_fuse.avfs.path_util import path_name
from studip_fuse.avfs.real_path import RealPath
from studip_fuse.studipfs.api.session import StudIPSession
from studip_fuse.studipfs.main_loop.start import setup_loop
from studip_fuse.studipfs.main_loop.stop import await_loop_thread_shutdown
from studip_fuse.studipfs.main_loop.ts_defaultdict import ThreadSafeDefaultDict
from studip_fuse.studipfs.path.studip_path import StudIPPath

ENOATTR = getattr(errno, "ENOATTR", getattr(errno, "ENODATA"))

log = logging.getLogger(__name__)
log_ops = log.getChild("ops")
cached_signature = functools.lru_cache(typed=True)(inspect.signature)


def log_status(status, args=None, level=logging.INFO):
    args = (status, *fuse_get_context(), os.getpid(), args.user if args else "?", args.mount if args else "?")
    logging.getLogger("studip_fuse.status").log(level, " ".join(["%s"] * len(args)), *args)


def syncify(asyncfun):
    @functools.wraps(asyncfun)
    def sync_wrapper(self, *args, **kwargs):
        return self.async_result(asyncfun, self, *args, **kwargs)

    return sync_wrapper


@attr.s(hash=False)
class FUSEView(object):
    args = attr.ib()
    http_args = attr.ib()
    fuse_args = attr.ib()

    loop_future = attr.ib(init=False, default=None)
    loop_thread = attr.ib(init=False, default=None)
    loop = attr.ib(init=False, default=None)  # type: BaseEventLoop
    session = attr.ib(init=False, default=None)  # type: StudIPSession
    root_rp = attr.ib(init=False, default=None)  # type: RealPath
    open_files = attr.ib(init=False, default=Factory(dict))  # type: Dict[str, Download]
    read_locks = attr.ib(init=False, repr=False, default=Factory(lambda: ThreadSafeDefaultDict(Lock)))

    @staticmethod
    def saferepr(val):
        val = pprint.saferepr(val)
        if len(val) > 2000:
            val = val[:1985] + "[...]" + val[-10:]
        return val

    def __call__(self, op, path, *args):
        if log_ops.isEnabledFor(logging.DEBUG):
            signature = cached_signature(getattr(self, op))
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

        # TODO make mockable: setup_loop for trio, StudIPPath for caching
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

        log_status("READY", args=self.args)
        log.info("Mounting complete")

    def destroy(self, path):
        log_status("STOPPING", args=self.args)
        log.info("Unmounting from %s (uid=%s, gid=%s, pid=%s, python pid=%s)", path, *fuse_get_context(),
                 os.getpid())

        if self.loop_future:
            self.loop_future.cancel()
        # TODO loop should be abstract / replaceable with trio loop, so no specific shut-down code here
        if self.loop and self.loop.is_running():
            self.loop.call_soon_threadsafe(self.loop.stop)
        if self.loop_thread:
            await_loop_thread_shutdown(self.loop, self.loop_thread)

        log.info("Unmounting complete")

    def async_result(self, corofn, *args, **kwargs):
        assert not inspect.iscoroutine(corofn)
        assert inspect.iscoroutinefunction(corofn)
        if not self.loop:
            raise RuntimeError("Can't await async operation while event loop isn't available")
        # TODO make this replaceable by e.g trio.BlockingTrioPortal.run(afn, *args)
        return asyncio.run_coroutine_threadsafe(corofn(*args, **kwargs), self.loop).result()

    @syncify
    async def _resolve(self, path: str) -> RealPath:
        return await self.root_rp.resolve(path)

    @syncify
    async def readdir(self, path, fh=None) -> List[str]:
        resolved_real_file = await self.root_rp.resolve(path)
        if not resolved_real_file:
            raise FuseOSError(errno.ENOENT)
        elif resolved_real_file.is_folder:
            return ['.', '..'] + [path_name(rp.path) for rp in await resolved_real_file.list_contents()]
        else:
            raise FuseOSError(errno.ENOTDIR)

    @syncify
    async def access(self, path, mode):
        resolved_real_file = await self.root_rp.resolve(path)
        if resolved_real_file:
            await resolved_real_file.access(mode)

    @syncify
    async def getattr(self, path, fh=None):
        resolved_real_file = await self.root_rp.resolve(path)
        if not resolved_real_file:
            raise FuseOSError(errno.ENOENT)
        else:
            return await resolved_real_file.getattr()

    @syncify
    async def getxattr(self, path, name, position=0):
        resolved_real_file = await self.root_rp.resolve(path)
        if not resolved_real_file:
            raise FuseOSError(errno.ENOENT)
        else:
            xattr = await resolved_real_file.getxattr()
            if name in xattr:
                return xattr[name]
            else:
                raise FuseOSError(ENOATTR)

    @syncify
    async def listxattr(self, path):
        resolved_real_file = await self.root_rp.resolve(path)
        if not resolved_real_file:
            raise FuseOSError(errno.ENOENT)
        else:
            xattr = await resolved_real_file.getxattr()
            return list(xattr.keys())

    def open(self, path, flags):
        resolved_real_file: RealPath = self._resolve(path)
        if not resolved_real_file:
            raise FuseOSError(errno.ENOENT)
        elif resolved_real_file.is_folder:
            raise FuseOSError(errno.EISDIR)
        else:
            download = self.async_result(resolved_real_file.open_file, flags)
            if os.name == 'nt' and not flags & getattr(os, "O_TEXT", 16384):
                flags |= os.O_BINARY
            fileno = os.open(download.local_path, flags)
            self.open_files[fileno] = download
            return fileno

    def read(self, path, length, offset, fh):
        download = self.open_files.get(fh, None)
        if download:
            self.async_result(download.await_readable, offset, length)

        with self.read_locks[fh]:
            os.lseek(fh, offset, os.SEEK_SET)
            return os.read(fh, length)

    def flush(self, path, fh):
        return os.fsync(fh)

    def fsync(self, path, fdatasync, fh):
        return self.flush(path, fh)

    def release(self, path, fh):
        self.open_files.pop(fh, None)
        return os.close(fh)

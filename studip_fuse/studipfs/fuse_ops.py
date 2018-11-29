import concurrent.futures
import errno
import functools
import inspect
import logging.handlers
import os
import pprint
import threading
from collections import defaultdict
from threading import Lock, Thread
from typing import Callable, Dict, List, NamedTuple

import attr
from attr import Factory

from studip_fuse.avfs.path_util import path_name
from studip_fuse.avfs.real_path import RealPath
from studip_fuse.launcher.fuse import FuseOSError, fuse_get_context
from studip_fuse.studipfs.api.aiointerface import Download

ENOATTR = getattr(errno, "ENOATTR", getattr(errno, "ENODATA"))

log = logging.getLogger(__name__)
log_ops = log.getChild("ops")
cached_signature = functools.lru_cache(typed=True)(inspect.signature)


def log_status(status, args=None, level=logging.INFO):
    args = (status, *fuse_get_context(), os.getpid(), args.user if args else "?", args.mount if args else "?")
    logging.getLogger("studip_fuse.status").log(level, " ".join(["%s"] * len(args)), *args)


def join_thread(loop_thread):
    import sys
    import traceback

    counter = 0
    while loop_thread.is_alive() and counter < 4:
        loop_thread.join(5)
        counter += 1
        if loop_thread.is_alive():
            log.info("Waiting for loop thread to abort...")
            if log.isEnabledFor(logging.DEBUG):
                stack = sys._current_frames()[loop_thread.ident]
                stack = stack[0] if isinstance(stack, list) else stack
                log.debug("Thread stack trace:\n %s", "".join(traceback.format_stack(stack)))

    if loop_thread.is_alive():
        log.warning("Shutting down main thread and thus killing hung event loop daemon thread")


LoopSetupResult = NamedTuple("LoopSetupResult", [
    ("loop_stop_fn", Callable),
    ("loop_run_fn", Callable),
    ("root_rp", RealPath),
])


def syncify(asyncfun):
    @functools.wraps(asyncfun)
    def sync_wrapper(self, *args, **kwargs):
        return self.loop_run_fn(asyncfun, self, *args, **kwargs)

    return sync_wrapper


@attr.s(hash=False)
class FUSEView(object):
    log_args = attr.ib()
    loop_setup_fn = attr.ib()

    loop_future = attr.ib(init=False, default=None)
    loop_thread = attr.ib(init=False, default=None)

    loop_stop_fn = attr.ib(init=False, default=None)
    loop_run_fn = attr.ib(init=False, default=None)
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
        log_status("INITIALIZING", args=self.log_args)
        log.info("Mounting at %s (uid=%s, gid=%s, pid=%s, python pid=%s)", path, *fuse_get_context(), os.getpid())

        self.loop_future = concurrent.futures.Future()
        self.loop_thread = Thread(target=self.loop_setup_fn, args=(self.loop_future,), name="aio event loop", daemon=True)
        self.loop_thread.start()

        log.debug("Event loop thread started, waiting for initialization to complete")
        self.loop_stop_fn, self.loop_run_fn, self.root_rp = self.loop_future.result()

        log_status("READY", args=self.log_args)
        log.info("Mounting complete")

    def destroy(self, path):
        log_status("STOPPING", args=self.log_args)
        log.info("Unmounting from %s (uid=%s, gid=%s, pid=%s, python pid=%s)", path, *fuse_get_context(),
                 os.getpid())

        if self.loop_future:
            self.loop_future.cancel()
        if self.loop_stop_fn:
            self.loop_stop_fn()
        if self.loop_thread:
            join_thread(self.loop_thread)

        log.info("Unmounting complete")

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
            download = self.loop_run_fn(resolved_real_file.open_file, flags)  # type: Download
            self.loop_run_fn(download.start_loading)  # TODO when / how to start?
            self.loop_run_fn(download.await_readable)
            if os.name == 'nt' and not flags & getattr(os, "O_TEXT", 16384):
                flags |= os.O_BINARY
            fileno = os.open(download.local_path, flags)
            self.open_files[fileno] = download
            return fileno

    def read(self, path, length, offset, fh):
        download = self.open_files.get(fh, None)
        if download:
            self.loop_run_fn(download.await_readable, offset, length)

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


class ThreadSafeDefaultDict(defaultdict):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.__lock = threading.Lock()

    def __missing__(self, key):
        with self.__lock:
            if key in self:
                return super().__getitem__(key)
            else:
                return super().__missing__(key)

import asyncio
import concurrent.futures
import errno
import inspect
import logging.handlers
import os
import pprint
import socket
import tempfile
from asyncio import BaseEventLoop
from stat import S_IFREG
from threading import Lock, Thread
from typing import Dict, List

import attr
from aiohttp import ServerDisconnectedError
from attr import Factory
from fuse import FUSE, FuseOSError, fuse_get_context

from studip_api.downloader import Download
from studip_fuse.__main__.main_loop import main_loop
from studip_fuse.__main__.thread_util import ThreadSafeDefaultDict, await_loop_thread_shutdown
from studip_fuse.cache import AsyncTaskCache, cached_task
from studip_fuse.path import RealPath, VirtualPath, path_name

log = logging.getLogger("studip_fuse.fs_driver")
log_ops = logging.getLogger("studip_fuse.fs_driver.ops")


def fuse_exit():
    from fuse import _libfuse, c_void_p

    fuse_ptr = c_void_p(_libfuse.fuse_get_context().contents.fuse)
    _libfuse.fuse_exit(fuse_ptr)

    # alternative without directly invoking native code
    # os.kill(os.getpid(), signal.SIGINT)


class FixedFUSE(FUSE):
    def __init__(self, operations: "FUSEView", mountpoint, **kwargs):
        self.__critical_exception = None
        super().__init__(operations, mountpoint, **kwargs)
        if self.__critical_exception:
            raise self.__critical_exception

    def _wrapper(self, func, *args, **kwargs):
        try:
            if func.__name__ == "init":
                # init may not fail, as its return code is just stored as private_data field of struct fuse_context
                return func(*args, **kwargs) or 0

            else:
                try:
                    return func(*args, **kwargs) or 0

                except (TimeoutError, asyncio.TimeoutError) as e:
                    log.debug("FUSE operation %s raised a %s, returning errno.ETIMEDOUT.",
                              func.__name__, type(e), exc_info=True)
                    return -errno.ETIMEDOUT

                except concurrent.futures.CancelledError as e:
                    log.debug("FUSE operation %s raised a %s, returning errno.ECANCELED.",
                              func.__name__, type(e), exc_info=True)
                    return -errno.ECANCELED

                except ServerDisconnectedError as e:
                    log.debug("FUSE operation %s raised a %s, returning errno.ECONNRESET.",
                              func.__name__, type(e), exc_info=True)
                    return -errno.ECONNRESET

                except (socket.gaierror, socket.herror) as e:
                    log.debug("FUSE operation %s raised a %s, returning errno.EHOSTUNREACH.",
                              func.__name__, type(e), exc_info=True)
                    return -errno.EHOSTUNREACH

                except OSError as e:
                    if e.errno > 0:
                        log.debug("FUSE operation %s raised a %s, returning errno %s.", func.__name__, type(e), exc_info=True)
                        return -e.errno
                    else:
                        log.error("FUSE operation %s raised an OSError with negative errno %s, returning errno.EINVAL.",
                                  func.__name__, e.errno, exc_info=True)
                        return -errno.EINVAL

                except Exception:
                    log.error("Uncaught exception from FUSE operation %s, returning errno.EINVAL.",
                              func.__name__, exc_info=True)
                    return -errno.EINVAL

        except BaseException as e:
            self.__critical_exception = e
            log.critical("Uncaught critical exception from FUSE operation %s, aborting.",
                         func.__name__, exc_info=True)
            # the raised exception (even SystemExit) will be caught by FUSE potentially causing SIGSEGV,
            # so tell system to stop/interrupt FUSE
            fuse_exit()
            return -errno.EFAULT


# FUSE Doc:             https://libfuse.github.io/doxygen/files.html
# FUSE Explanation:     https://lastlog.de/misc/fuse-doc/doc/html/
# FUSE Functions Info:  https://www.cs.hmc.edu/~geoff/classes/hmc.cs135.201001/homework/fuse/fuse_doc.html
# Linux System Errors:  http://www-numi.fnal.gov/offline_software/srt_public_context/WebDocs/Errors/unix_system_errors.html
@attr.s(hash=False)
class FUSEView(object):
    args = attr.ib()
    http_args = attr.ib()
    fuse_args = attr.ib()

    loop_future = attr.ib(init=False, default=None)
    loop_thread = attr.ib(init=False, default=None)
    loop = attr.ib(init=False, default=None)  # type: BaseEventLoop
    session = attr.ib(init=False, default=None)
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

    def destroy(self, path):
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
            msg = self.schedule_async(AsyncTaskCache.clear_all_caches()).result()
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

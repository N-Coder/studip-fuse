import asyncio
import concurrent.futures
import errno
import inspect
import socket
from asyncio import Future
from itertools import chain
from typing import Dict, Union

import aiohttp
import attr

from studip_fuse.cache import AsyncCache, CachedValue, CachedValueNotAvailableError, KeyType

__all__ = ["guess_errno_from_exception", "AsyncModelCache", "AsyncDownloadCache"]


def guess_errno_from_exception(exc: Union[Future, Exception]):
    if isinstance(exc, Future):
        exc = exc.exception()

    msg = str(exc)
    while exc:
        if isinstance(exc, concurrent.futures.TimeoutError):
            return errno.ETIMEDOUT, msg
        elif isinstance(exc, concurrent.futures.CancelledError):
            return errno.ECANCELED, msg
        elif isinstance(exc, aiohttp.ServerDisconnectedError):
            return errno.ECONNRESET, msg
        elif isinstance(exc, (socket.gaierror, socket.herror)):
            return errno.EHOSTUNREACH, msg
        elif isinstance(exc, aiohttp.ClientResponseError):
            if exc.code == 403:
                return errno.EACCES, exc.message
            elif exc.code in [404, 410]:
                return errno.ENOENT, exc.message
        elif isinstance(exc, OSError) and exc.errno > 0:
            return exc.errno, msg
        exc = exc.__cause__

    return errno.EINVAL, "error with unknown error code: %s" % msg


def is_permanent_exception(exc):
    if isinstance(exc, Future):
        exc = exc.exception()

    if isinstance(exc, aiohttp.ClientResponseError) and 400 <= exc.code < 500:
        return True
    elif isinstance(exc, OSError) and exc.errno in (errno.EEXIST, errno.EISDIR, errno.ENOTDIR, errno.ENOENT):
        return True
    else:
        return False


def no_cache_value_error_from_history(attempts_exc_history, cache_msg):
    if attempts_exc_history:
        # if there is anything usable in the recent exc history, use that instead of timeout / network down
        for cv in reversed(attempts_exc_history):
            if cv.future.done() and not cv.future.cancelled() and cv.future.exception():
                oserrno, errstr = guess_errno_from_exception(cv.future)
                exc = CachedValueNotAvailableError(oserrno, errstr, cache_msg)
                exc.__cause__ = cv.future.exception()
                return exc

    return None


@attr.s()
class AsyncModelCache(AsyncCache):
    def make_key(self, args, kwargs) -> KeyType:
        keys = list(chain(args, kwargs.values()))
        if len(keys) == 0:
            return ""
        else:
            assert len(keys) == 1
            from studip_api.model import ModelClass
            assert isinstance(keys[0], ModelClass)
            return keys[0].id

    def no_cache_value_error(self, attempts_exc_history, attempts_timed_out, cache_msg):
        hist_exc = no_cache_value_error_from_history(attempts_exc_history, cache_msg)
        if hist_exc:
            raise hist_exc
        else:
            raise super().no_cache_value_error(attempts_exc_history, attempts_timed_out, cache_msg)

    def make_cache_value(self, task: Future, **context):
        return CachedModelClass(task)

    def update_cache(self, export_data: Dict, overwrite: bool):
        raise NotImplementedError("cache persistance not available for %s" % type(self).__name__)

    def export_cache(self) -> Dict:
        raise NotImplementedError("cache persistance not available for %s" % type(self).__name__)


@attr.s(frozen=True, slots=True)
class CachedModelClass(CachedValue):
    def is_valid(self) -> bool:
        if self.future is None or not self.future.done() or self.future.cancelled():
            return False
        if self.future.exception():
            return is_permanent_exception(self.future.exception())
        else:
            return True


@attr.s()
class AsyncDownloadCache(AsyncCache):
    def make_key(self, args, kwargs):
        arguments = inspect.signature(self.wrapped_function).bind(*args, **kwargs)  # type: BoundArguments
        arguments.apply_defaults()
        return arguments.arguments["studip_file"], arguments.arguments["local_dest"]

    def start_task(self, func_args, func_kwargs, old_value=None, _key=None, **context):
        from studip_api.downloader import log, Download

        if old_value and old_value.is_available():
            old_task = old_value.future
            assert asyncio.isfuture(old_task) and old_task.done(), \
                "Can't create a new cached download task when old task is in invalid state: %s" % old_value
            if old_task.done() and not old_task.exception() and not old_task.cancelled():
                assert isinstance(old_task.result(), Download), \
                    "Expected result of old cached download task to be a Download, but it was %s" % old_value.result()
                return asyncio.ensure_future(old_task.result().fork())
            else:
                log.debug("Previous download for %s failed, retrying task %s instead of forking.", _key, old_value)

        return super().start_task(func_args, func_kwargs)

    def no_cache_value_error(self, attempts_exc_history, attempts_timed_out, cache_msg):
        hist_exc = no_cache_value_error_from_history(attempts_exc_history, cache_msg)
        if hist_exc:
            raise hist_exc
        else:
            raise super().no_cache_value_error(attempts_exc_history, attempts_timed_out, cache_msg)

    def make_cache_value(self, task: Future, **context):
        return CachedDownload(task)


@attr.s(frozen=True, slots=True)
class CachedDownload(CachedValue):
    def is_pending(self):
        return super().is_pending() or \
               (super().is_valid() and not self.future.result().completed.done())

    def is_valid(self):
        if self.future is None or not self.future.done() or self.future.cancelled():
            return False
        elif self.future.exception():
            return is_permanent_exception(self.future.exception())
        else:
            download_completed = self.future.result().completed
            return download_completed.done() and not (download_completed.cancelled() or download_completed.exception())

    def should_reattempt(self):
        if self.future is None:
            return True
        # is_pending might still be true, as it also includes the content download for CachedDownloads
        assert self.future.done(), "Can't reattempt while old future %s is still pending" % self.future

        if self.future.cancelled():
            return True
        elif self.future.exception():
            return not (is_permanent_exception(self.future.exception()) or isinstance(self.future.exception(), CachedValueNotAvailableError))
        else:
            # Download has been started, don't start a new one
            return False

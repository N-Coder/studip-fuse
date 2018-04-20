import asyncio
import concurrent.futures
import errno
import functools
import inspect
import logging
import socket
from asyncio import Future
from collections import defaultdict
from inspect import BoundArguments
from itertools import chain
from posix import strerror
from typing import Any, Callable, Dict, List, Optional, Union

import aiohttp
import attr
from async_timeout import timeout
from attr import Factory
from attr.validators import instance_of, optional
from cached_property import cached_property

__all__ = ["KeyType", "CachedValueNotAvailableError", "guess_errno_from_exception", "CachedValue", "AsyncCache",
           "cached_task", "AsyncModelCache", "AsyncDownloadCache", "CachedDownload"]
log = logging.getLogger("studip_fuse.async_cache")
KeyType = str


class CachedValueNotAvailableError(OSError):
    def __init__(self, errno, errstr=None, cache_msg=None):
        if not errstr:
            errstr = strerror(errno)
        super(CachedValueNotAvailableError, self).__init__(errno, errstr, cache_msg)


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
        elif isinstance(exc, OSError) and exc.errno > 0:
            return exc.errno, msg
        exc = exc.__cause__

    return errno.EINVAL, "error with unknown error code"


def is_permanent_exception(exc):
    if isinstance(exc, Future):
        exc = exc.exception()

    if isinstance(exc, aiohttp.ClientResponseError) and 400 <= exc.code < 500:
        return True
    elif isinstance(exc, OSError) and exc.errno in (errno.EEXIST, errno.EISDIR, errno.ENOTDIR, errno.ENOENT):
        return True
    else:
        return False


@attr.s(frozen=True, slots=True)
class CachedValue(object):
    future = attr.ib(validator=optional(instance_of(Future)))  # type: Optional[Future]
    time = attr.ib(default=Factory(lambda: asyncio.get_event_loop().time()))  # type: float

    def is_available(self):
        return self.future is not None

    def is_pending(self) -> bool:
        return self.future is not None and not self.future.done()

    def is_valid(self) -> bool:
        if self.future is None or not self.future.done() or self.future.cancelled():
            return False
        if self.future.exception():
            return is_permanent_exception(self.future.exception())
        else:
            return True

    def is_fresh(self, timeout: float) -> bool:
        return self.future is not None and timeout >= 0 and asyncio.get_event_loop().time() - self.time < timeout


@attr.s()
class AsyncCache(object):
    wrapped_function = attr.ib()  # type: Callable
    may_attempt = attr.ib(default=None)  # type: Callable[[Any,...], bool]
    on_new_value_fetched = attr.ib(default=None)  # type: Callable[[CachedValue,Any,...], None]

    cache_timeout = attr.ib(default=600)  # type: float
    load_timeout = attr.ib(default=30)  # type: float
    max_load_attempts = attr.ib(default=3)  # type: int
    fallback_timeout = attr.ib(default=3600)  # type: float

    cache = attr.ib(init=False, repr=False)  # type: Dict[KeyType, CachedValue]
    fallbacks = attr.ib(init=False, repr=False)  # type: Dict[KeyType, CachedValue]

    def __attrs_post_init__(self):
        log.warning("Decorated %s with %s %s", self.wrapped_function, type(self).__name__, id(self))

    @cache.default
    @fallbacks.default
    def __cache_dict(self):
        return defaultdict(lambda: self.no_cache_value)

    def make_key(self, args, kwargs) -> KeyType:
        import functools
        return functools._make_key(args=args, kwds=kwargs, typed=False)

    def start_task(self, func_args, func_kwargs, **context) -> Future:
        assert context.get("func", self.wrapped_function) is self.wrapped_function
        return asyncio.ensure_future(self.wrapped_function(*func_args, **func_kwargs))

    def make_cache_value(self, task: Future, **context) -> CachedValue:
        return CachedValue(task)

    @cached_property
    def no_cache_value(self) -> CachedValue:
        return CachedValue(None)

    async def __await_cached_task(self, key, trace):
        # try to use valid and fresh cached value
        cached_task = self.cache[key]
        cached_task_is_fresh = cached_task.is_fresh(self.cache_timeout)
        if cached_task.is_pending():
            await cached_task.future
            return cached_task
        elif cached_task.is_valid():
            if cached_task_is_fresh:
                await cached_task.future
                return cached_task
            else:
                self.fallbacks[key] = cached_task
                trace("Cached task was valid, but expired.")
        elif cached_task_is_fresh:
            trace("Cached task is invalid, but was created recently.")
        elif not cached_task.is_available():
            trace("Cached task was never set.")
        else:
            trace("Cached task is invalid and expired.")

        return None

    async def __attempt_await_new_task(self, key, args, kwargs, trace):
        # try to get a new value
        attempts_exc_history = []  # type: List[Future]
        attempts_timed_out = False
        cache_value = None
        context = {
            "func": self.wrapped_function,
            "func_args": args,
            "func_kwargs": kwargs,
            "attempts_history": attempts_exc_history,
            "max_attempts": self.max_load_attempts,
            "old_value": self.fallbacks[key],
            "_async_cache": self,
            "_key": key
        }

        with timeout(self.load_timeout) as timer:
            context["timer"] = timer
            while (not self.may_attempt or self.may_attempt(**context)) \
                    and len(attempts_exc_history) < self.max_load_attempts:

                log.debug("%s %s started task %s[%s] with (*%s, **%s)", type(self).__name__, id(self), self.wrapped_function, key, args, kwargs)
                new_task = self.start_task(**context)
                try:
                    await asyncio.shield(new_task)  # protected from timeout cancellation
                except BaseException:
                    if timer.expired:
                        log.debug("%s %s task %s[%s] timed out", type(self).__name__, id(self), self.wrapped_function, key, exc_info=True)
                        self.cache[key] = self.make_cache_value(new_task, **context)  # let task finish in the background
                        attempts_exc_history.append(new_task)  # might not be done yet, but still record
                        attempts_timed_out = True
                        break
                    else:
                        log.debug("%s %s task %s[%s] failed", type(self).__name__, id(self), self.wrapped_function, key, exc_info=True)
                        attempts_exc_history.append(new_task)
                        continue
                else:  # no exception raised from `await`
                    # task was successful, so remove indirection to current task and store value
                    log.debug("%s %s task %s[%s] succeeded", type(self).__name__, id(self), self.wrapped_function, key)
                    cache_value = self.cache[key] = self.make_cache_value(new_task, **context)
                    if self.on_new_value_fetched:
                        self.on_new_value_fetched(cache_value, **context)
                    break

        trace("Tried %s times to get a new value, but attempts %s.",
              len(attempts_exc_history),
              "timed out" if attempts_timed_out else "were no longer allowed")
        trace("Attempts were: %s", attempts_exc_history)
        return cache_value, attempts_exc_history, attempts_timed_out

    def __get_fallback_task(self, key, trace):
        # fallback to a previously valid, but expired value
        fallback_task = self.fallbacks[key]
        fallback_task_is_fresh = fallback_task.is_fresh(self.fallback_timeout)
        fallback_task_is_valid = fallback_task.is_valid()
        if fallback_task_is_valid and fallback_task_is_fresh:
            # a fallback value must be done and may not contain an exception
            return fallback_task
        elif fallback_task_is_valid:
            trace("Fallback task was valid, but expired.")
        elif fallback_task_is_fresh:
            trace("Fallback task is invalid, but was created recently.")
        elif not fallback_task.is_available():
            trace("Fallback task was never set.")
        else:
            trace("Fallback task is invalid and expired.")

    def __call__(self, *args, __only_cached=False, **kwargs):
        if __only_cached:
            value = self.cache[self.make_key(args, kwargs)]
            if value.is_available():
                return value.future
            else:
                return None
        else:
            return self.await_cached_new_or_fallback_task(*args, **kwargs)

    async def await_cached_new_or_fallback_task(self, *args, **kwargs):
        key = self.make_key(args, kwargs)
        trace_data = []
        trace = lambda *x: trace_data.append(x)

        # try the various methods for obtaining a value, which raise StopIteration to report a valid value
        cache_value = await self.__await_cached_task(key, trace)
        if cache_value:
            # log.debug("%s %s request %s[%s] fulfilled from cache", type(self).__name__, id(self),  self.wrapped_function, key)
            return cache_value.future.result()

        # further requests should wait until this task finished trying to obtain a new value
        self.cache[key] = self.make_cache_value(asyncio.Task.current_task())

        cache_value, attempts_exc_history, attempts_timed_out = await self.__attempt_await_new_task(key, args, kwargs, trace)
        if cache_value:
            log.debug("%s %s request %s[%s] fulfilled after %s failed attempts",
                      type(self).__name__, id(self), self.wrapped_function, key, len(attempts_exc_history))
            return cache_value.future.result()

        cache_value = self.__get_fallback_task(key, trace)
        if cache_value:
            log.debug("%s %s request %s[%s] fulfilled from fallback cache",
                      type(self).__name__, id(self), self.wrapped_function, key)
            return cache_value.future.result()

        # raise an Exception with an appropriate errno
        # an exception from a cached / fallback task will never be rethrown, so try to retrace exception from try_fetch_new_value
        cache_msg = " ".join(t[0] % t[1:] for t in trace_data)
        log.warning("%s %s request %s[%s] failed: %s", type(self).__name__, id(self), self.wrapped_function, key, cache_msg)
        if attempts_exc_history:
            # if there is anything usable in the recent exc history, use that instead of timeout / network down
            for f in reversed(attempts_exc_history):
                if f.done() and not f.cancelled() and f.exception():
                    oserrno, errstr = guess_errno_from_exception(f)
                    raise CachedValueNotAvailableError(oserrno, errstr, cache_msg) from f.exception()

        if attempts_timed_out:
            raise CachedValueNotAvailableError(errno.ETIMEDOUT, cache_msg)
        else:
            raise CachedValueNotAvailableError(errno.ENETDOWN, cache_msg)


def cached_task():
    def wrapper(f):
        cache = AsyncCache(wrapped_function=f, max_load_attempts=1)

        # can't call the AsyncCache directly, need to wrap in in a function that can be modified by update_wrapper
        def cached(*args, **kwargs):
            return cache(*args, **kwargs)

        return functools.update_wrapper(cached, f)

    return wrapper


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

    def update_cache(self, export_data: Dict, overwrite: bool):
        raise NotImplementedError("cache persistance not available for %s" % type(self).__name__)

    def export_cache(self) -> Dict:
        raise NotImplementedError("cache persistance not available for %s" % type(self).__name__)


@attr.s()
class AsyncDownloadCache(AsyncCache):
    def make_key(self, args, kwargs):
        arguments = inspect.signature(self.wrapped_function).bind(*args, **kwargs)  # type: BoundArguments
        arguments.apply_defaults()
        return arguments.arguments["studip_file"], arguments.arguments["local_dest"]

    def start_task(self, func_args, func_kwargs, old_value=None, **context):
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
                log.debug("Previous download for %s failed, retrying task %s instead of forking.", key, old_value)

        return super().start_task(func_args, func_kwargs)

    def make_cache_value(self, task: Future, **context):
        return CachedDownload(task)


@attr.s(frozen=True, slots=True)
class CachedDownload(CachedValue):
    def is_pending(self):
        return super().is_pending() or \
               (super().is_valid() and not self.future.result().completed.done())

    def is_valid(self):
        if not super().is_valid():
            return False
        download_completed = self.future.result().completed
        return download_completed.done() and not (download_completed.cancelled() or download_completed.exception())

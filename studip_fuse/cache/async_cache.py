import asyncio
import errno
import functools
import logging
from asyncio import Future
from collections import defaultdict
from posix import strerror
from typing import Any, Callable, Dict, List, Optional

import attr
from async_timeout import timeout
from attr import Factory
from attr.validators import instance_of, optional
from cached_property import cached_property

__all__ = ["KeyType", "CachedValueNotAvailableError", "CachedValue", "AsyncCache", "cached_task"]
log = logging.getLogger("studip_fuse.async_cache")
KeyType = str


class CachedValueNotAvailableError(OSError):
    def __init__(self, errno, errstr=None, cache_msg=None):
        if not errstr:
            errstr = strerror(errno)
        super(CachedValueNotAvailableError, self).__init__(errno, errstr, cache_msg)


@attr.s(frozen=True, slots=True)
class CachedValue(object):
    future = attr.ib(validator=optional(instance_of(Future)))  # type: Optional[Future]
    # XXX time is the time of creation, not the time of completion
    time = attr.ib(default=Factory(lambda: asyncio.get_event_loop().time()))  # type: float

    def is_available(self):
        return self.future is not None

    def is_pending(self) -> bool:
        return self.future is not None and not self.future.done()

    def is_valid(self) -> bool:
        return self.future is not None and self.future.done() \
               and not (self.future.cancelled() or self.future.exception())

    def should_reattempt(self) -> bool:
        if self.future is None:
            return True
        assert not self.is_pending(), "Can't reattempt while old future %s is still pending" % self.future

        # don't reattempt execution if wrapped task failed after already doing multiple attempts
        return not (self.is_valid() or isinstance(self.future.exception(), CachedValueNotAvailableError))

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
        attempts_exc_history = []  # type: List[CachedValue]
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

                log.debug("%s %s started task %s[%s] with (*%s, **%s)",
                          type(self).__name__, id(self), self.wrapped_function, key, args, kwargs)
                cache_value = self.make_cache_value(self.start_task(**context), **context)
                try:
                    await asyncio.shield(cache_value.future)  # protected from timeout cancellation
                except BaseException:
                    if timer.expired:
                        log.debug("%s %s task %s[%s] timed out",
                                  type(self).__name__, id(self), self.wrapped_function, key, exc_info=True)
                        self.cache[key] = cache_value  # let task finish in the background
                        attempts_exc_history.append(cache_value)  # might not be done yet, but still record
                        cache_value = None  # don't return this value
                        attempts_timed_out = True
                        break
                    else:
                        pass  # exception will be handled later

                # XXX for Downloads is_pending is still True after the await here completed, so we can't make
                # any assumptions about is_valid here and only use should_reattempt

                if cache_value.should_reattempt():  # the operation failed temporarily, retry
                    if cache_value.future.cancelled():
                        log.debug("%s %s task %s[%s] was cancelled and should reattempt",
                                  type(self).__name__, id(self), self.wrapped_function, key)
                    elif cache_value.future.exception():
                        log.debug("%s %s task %s[%s] raised an exception and should reattempt",
                                  type(self).__name__, id(self), self.wrapped_function, key, exc_info=cache_value.future.exception())
                    else:
                        log.debug("%s %s task %s[%s] returned an invalid result and should reattempt",
                                  type(self).__name__, id(self), self.wrapped_function, key)

                    attempts_exc_history.append(cache_value)
                    cache_value = None  # don't return this value
                    continue

                else:  # the returned value / raised exception is accepted as a result
                    if cache_value.future.cancelled():
                        log.debug("%s %s task %s[%s] was cancelled, which was accepted as a valid result",
                                  type(self).__name__, id(self), self.wrapped_function, key)
                    elif cache_value.future.exception():
                        log.debug("%s %s task %s[%s] raised an exception, which was accepted as a valid result",
                                  type(self).__name__, id(self), self.wrapped_function, key, exc_info=cache_value.future.exception())
                    else:
                        log.debug("%s %s task %s[%s] returned a valid result",
                                  type(self).__name__, id(self), self.wrapped_function, key)

                    self.cache[key] = cache_value
                    if self.on_new_value_fetched:
                        self.on_new_value_fetched(cache_value, **context)
                    break

        trace("Tried %s times to get a new value, but attempts %s.",
              len(attempts_exc_history),
              "timed out" if attempts_timed_out else "were no longer allowed")
        trace("Attempts were: %s", attempts_exc_history)
        return cache_value, attempts_exc_history, attempts_timed_out

    def __get_fallback_task(self, key, trace):
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

    def no_cache_value_error(self, attempts_exc_history, attempts_timed_out, cache_msg):
        if attempts_timed_out:
            raise CachedValueNotAvailableError(errno.ETIMEDOUT, cache_msg)
        else:
            raise CachedValueNotAvailableError(errno.ENETDOWN, cache_msg)

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

        # try the various methods for obtaining a value, which return the valid CachedValue if found / obtained
        # try to use valid and fresh cached value
        cache_value = await self.__await_cached_task(key, trace)
        if cache_value:
            # log.debug("%s %s request %s[%s] fulfilled from cache", type(self).__name__, id(self),  self.wrapped_function, key)
            return cache_value.future.result()

        # further requests should wait until this task finished trying to obtain a new value (LOCK)
        self.cache[key] = current_task = self.make_cache_value(asyncio.Task.current_task())
        # attempt to get a new value
        cache_value, attempts_exc_history, attempts_timed_out = await self.__attempt_await_new_task(key, args, kwargs, trace)
        if cache_value:
            log.debug("%s %s request %s[%s] fulfilled after %s failed attempts",
                      type(self).__name__, id(self), self.wrapped_function, key, len(attempts_exc_history))
            return cache_value.future.result()
        # if cache value wasn't changed by attempts, discard ref to current task, otherwise fallback value would be propagated to cache again
        if self.cache[key] is current_task:
            del self.cache[key]

        # fallback to a previously valid, but expired value
        cache_value = self.__get_fallback_task(key, trace)
        if cache_value:
            log.debug("%s %s request %s[%s] fulfilled from fallback cache",
                      type(self).__name__, id(self), self.wrapped_function, key)
            return cache_value.future.result()

        # raise an Exception with an appropriate errno
        # an exception from a cached / fallback task will never be rethrown, so try to retrace exception from try_fetch_new_value
        cache_msg = " ".join(t[0] % t[1:] for t in trace_data)
        log.warning("%s %s request %s[%s] failed: %s", type(self).__name__, id(self), self.wrapped_function, key, cache_msg)
        raise self.no_cache_value_error(attempts_exc_history, attempts_timed_out, cache_msg)


def cached_task(cache_class=AsyncCache, **kwargs):
    def wrapper(f):
        cache = cache_class(wrapped_function=f, **kwargs)

        # can't call the AsyncCache directly, need to wrap it in a function that can be modified by update_wrapper
        def cached(*args, **kwargs):
            return cache(*args, **kwargs)

        return functools.update_wrapper(cached, f)

    return wrapper

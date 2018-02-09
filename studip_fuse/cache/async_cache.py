import asyncio
import functools
import inspect
import logging
import sys
from datetime import datetime
from threading import current_thread
from typing import Any, Dict, NamedTuple

from cached_property import cached_property

__all__ = ["CacheInfo", "CallInfo", "CoroCallCounter", "CoroCallCounterClass", "CoroWrapper", "CoroWrapperClass",
           "cached_future_validator", "AsyncTaskCache", "AsyncTaskCacheClass", "clear_caches", "cached_task"]

async_cache_log = logging.getLogger("studip_fuse.async_cache")

CacheInfo = NamedTuple("CacheInfo", [("hits", int), ("misses", int), ("cache_len", int), ])
CallInfo = NamedTuple("CallInfo", [("call_counter", int), ("pending", int), ("successful", int), ("failed", int), ])


class DecoratorClass(object):
    def __init__(self, user_func):
        functools.update_wrapper(self, user_func)
        assert self.__wrapped__ == user_func

    def __getattr__(self, item):
        return getattr(self.__wrapped__, item)

    def __get__(self, obj, type=None):
        # see https://stackoverflow.com/questions/47433768#comment81822552_47433786
        return functools.partial(self, obj)


def CoroCallCounter():
    return CoroCallCounterClass


class CoroCallCounterClass(DecoratorClass):
    def __init__(self, user_func):
        super().__init__(user_func)

        self.__call_counter = 0
        self.__successful_calls = 0
        self.__failed_calls = 0

    def call_info(self):
        return CallInfo(self.__call_counter, (self.__call_counter - self.__successful_calls - self.__failed_calls),
                        self.__successful_calls, self.__failed_calls)

    def __call__(self, *args, **kwargs):
        return self.__schedule_task(*args, **kwargs)

    def __schedule_task(self, *args, **kwargs):
        self.__call_counter += 1
        my_call_counter = self.__call_counter
        if async_cache_log.isEnabledFor(logging.DEBUG):
            async_cache_log.debug(
                "Scheduling %s#%s: %s%s from thread %s",
                self.__wrapped__.__name__, my_call_counter, self.__wrapped__,
                inspect.signature(self.__wrapped__).bind(*args, **kwargs), current_thread())
        coro = self.__call_async(my_call_counter, *args, **kwargs)
        async_cache_log.debug("Scheduled %s#%s as %s", self.__wrapped__.__name__, my_call_counter, coro)
        return coro

    async def __call_async(self, my_call_counter, *args, **kwargs):
        async_cache_log.debug("Started execution of %s#%s", self.__wrapped__.__name__, my_call_counter)
        try:
            result = await self.__wrapped__(*args, **kwargs)
            async_cache_log.debug("Completed execution of %s#%s = %s", self.__wrapped__.__name__, my_call_counter,
                                  result)
            self.__successful_calls += 1
            return result
        except:
            async_cache_log.debug("Execution of %s#%s failed with %s", self.__wrapped__.__name__, my_call_counter,
                                  sys.exc_info()[1])
            self.__failed_calls += 1
            raise


def CoroWrapper(wrap_with):
    return functools.partial(CoroCallCounterClass, wrap_with=wrap_with)


class CoroWrapperClass(DecoratorClass):
    def __init__(self, user_func, wrap_with):
        super().__init__(user_func)
        self.__wrap_with = wrap_with

    def __call__(self, *args, **kwargs):
        return self.__wrap_with(self.__wrapped__(*args, **kwargs))


def cached_future_validator(key, val):
    if asyncio.isfuture(val):
        if not val.done():
            # use future result
            return val
        elif val.cancelled() or val.exception():
            # don't reuse failed tasks
            return None
        else:
            # reuse result of successful tasks
            return val
    else:
        assert val is None
        return val


def AsyncTaskCache(cached_value_validator=None):
    return functools.partial(AsyncTaskCacheClass, cached_value_validator)


class AsyncTaskCacheClass(DecoratorClass):
    def __init__(self, user_func, cached_value_validator=None):
        super().__init__(user_func)
        self.cached_value_validator = cached_value_validator or cached_future_validator

        self.__hits = 0
        self.__misses = 0

        self.__cache = {}  # type: Dict[Any, asyncio.Future]

    @cached_property
    def __lock(self):
        # initialize lazy, so that asyncio.get_event_loop() doesn't create a new event loop before the actual one is set
        return asyncio.Lock()

    def cache_info(self):
        return CacheInfo(self.__hits, self.__misses, len(self.__cache))

    async def cache_clear(self):
        async with self.__lock:
            self.__cache.clear()
            self.__hits = self.__misses = 0

    def __call__(self, *args, **kwargs):
        return self.__get_cached_task(*args, **kwargs)

    def __get_valid_cache_value(self, key):
        return self.cached_value_validator(key, self.__cache.get(key, None))

    async def __get_cached_task(self, *args, **kwargs):
        from functools import _make_key as make_key
        key = make_key(args, kwargs, typed=False)

        res = self.__get_valid_cache_value(key)
        if res is not None:
            self.__hits += 1
            return await res

        async with self.__lock:
            res = self.__get_valid_cache_value(key)
            if res is not None:
                self.__hits += 1
                return await res

            res = self.__wrapped__(*args, **kwargs)
            if not asyncio.isfuture(res):
                raise RuntimeError("Expected result of user function %s to be an asyncio.Future, "
                                   "but got '%s' of type %s" % (
                                       self.__wrapped__, res, res.__class__ if res else None))
            self.__cache[key] = res
            self.__misses += 1
            return await res


last_cache_clear = datetime.now()
cached_tasks = []


async def clear_caches():
    global last_cache_clear, cached_tasks

    async_cache_log.warning("Clearing caches...")
    msg = "Clearing cache of %s tasks. Last clear was %s s ago at %s.\n" % \
          (len(cached_tasks), datetime.now() - last_cache_clear, last_cache_clear)

    for task in cached_tasks:
        msg += "Statistics for task %s:\n" \
               "\tCalls: %s\n" \
               "\tCache: %s\n" % (task.__name__, getattr(task, "call_info", lambda: "???")(), task.cache_info())
        await task.cache_clear()

    last_cache_clear = datetime.now()
    msg = msg.strip()
    async_cache_log.info(msg)
    return msg


def cached_task(cached_value_validator=None):
    def wrapper(user_func):
        async_cache_log.debug(
            "Scheduling future execution of coroutine (result of calling) %s and caching successful executions",
            user_func)
        wrapped = CoroCallCounterClass(user_func)

        wrapped = CoroWrapperClass(wrapped, asyncio.ensure_future)
        wrapped = AsyncTaskCacheClass(wrapped, cached_value_validator)
        cached_tasks.append(wrapped)
        return wrapped

    return wrapper

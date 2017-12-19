import asyncio
import concurrent.futures
import functools
import inspect
import logging
import sys
from typing import Any, Dict, NamedTuple, Union

async_cache_log = logging.getLogger("studip_fuse.async_cache")

FUTURE_TYPES = (concurrent.futures.Future, asyncio.Future)
CacheInfo = NamedTuple("CacheInfo", [("hits", int), ("misses", int), ("cache_len", int), ])
CallInfo = NamedTuple("CallInfo", [("call_counter", int), ("pending", int), ("successful", int), ("failed", int), ])


class DecoratorClass(object):
    def __get__(self, obj, type=None):
        # see https://stackoverflow.com/questions/47433768#comment81822552_47433786
        return functools.partial(self, obj)


class TaskScheduler(DecoratorClass):
    def __init__(self, user_func, schedule_with):
        # this must be defined here and not in the parent class, so that functools.update_wrapper doesn't overwrite
        # it when nesting DecoratorClasses
        self.__user_func = user_func
        self.__schedule_with = schedule_with

        self.__call_counter = 0
        self.__successful_calls = 0
        self.__failed_calls = 0

        functools.update_wrapper(self, self.__user_func)

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
                "Scheduling %s#%s: %s(%s%s)",
                self.__user_func.__name__, my_call_counter, self.__schedule_with.__name__,
                self.__user_func, inspect.signature(self.__user_func).bind(*args, **kwargs))
        future = self.__schedule_with(self.__call_async(my_call_counter, *args, **kwargs))
        # async_cache_log.debug("Scheduled #%s as %s", my_call_counter, future)
        return future

    async def __call_async(self, my_call_counter, *args, **kwargs):
        async_cache_log.debug("Started execution of %s#%s", self.__user_func.__name__, my_call_counter)
        try:
            result = await self.__user_func(*args, **kwargs)
            async_cache_log.debug("Completed execution of %s#%s = %s", self.__user_func.__name__, my_call_counter,
                                  result)
            self.__successful_calls += 1
            return result
        except:
            async_cache_log.debug("Execution of %s#%s failed with %s", self.__user_func.__name__, my_call_counter,
                                  sys.exc_info()[1])
            self.__failed_calls += 1
            raise


class TaskCache(DecoratorClass):
    def __init__(self, user_func, lock_with):
        self.__user_func = user_func
        self.__lock_with = lock_with

        self.__hits = 0
        self.__misses = 0

        self.__cache = {}  # type: Dict[Any, Union[concurrent.futures.Future, asyncio.Future]]
        self.__lock = self.__lock_with()

        functools.update_wrapper(self, self.__user_func)

    def cache_info(self):
        return CacheInfo(self.__hits, self.__misses, len(self.__cache))

    def cache_clear(self):
        with self.__lock:
            self.__cache.clear()
            self.__hits = self.__misses = 0

    def __call__(self, *args, **kwargs):
        return self.__get_cached_task(*args, **kwargs)

    def __get_valid_cache_value(self, key):
        val = self.__cache.get(key, None)
        if isinstance(val, FUTURE_TYPES):
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

    def __get_cached_task(self, *args, **kwargs):
        from functools import _make_key as make_key
        key = make_key(args, kwargs, typed=False)

        res = self.__get_valid_cache_value(key)
        if res is not None:
            self.__hits += 1
            return res

        with self.__lock:
            res = self.__get_valid_cache_value(key)
            if res is not None:
                self.__hits += 1
                return res

            res = self.__user_func(*args, **kwargs)
            if not isinstance(res, FUTURE_TYPES):
                raise RuntimeError("Expected result of user function %s to be of type %s, but got '%s' of type %s" % (
                    self.__user_func, FUTURE_TYPES, res, res.__class__ if res else None))
            self.__cache[key] = res
            self.__misses += 1
            return res


cached_tasks = []


class called_in_loop(object):
    def __enter__(self):
        if asyncio.get_event_loop().get_debug():
            asyncio.get_event_loop()._check_thread()
        return None

    def __exit__(self, exc_type, exc_val, exc_tb):
        pass


# TODO refactor multi-level caching (esp. in FUSEView), add ttl / SIGUSR-based clearing
# TODO check Handling of exceptions
def cached_task(schedule_with=asyncio.ensure_future, lock_with=called_in_loop):
    def wrapper(user_func):
        async_cache_log.debug(
            "Scheduling future execution of coroutine (result of calling) %s with %s and caching successful executions "
            "(guarded by %s)", user_func, schedule_with, lock_with)

        scheduled = TaskScheduler(user_func, schedule_with)
        cached = TaskCache(scheduled, lock_with)
        cached_tasks.append(cached)
        return cached

    return wrapper

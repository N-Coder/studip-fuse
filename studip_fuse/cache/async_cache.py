import asyncio
import functools
import inspect
import logging
import sys
from datetime import datetime
from threading import current_thread
from time import time
from typing import Any, Dict

from cached_property import cached_property
from tabulate import tabulate

from studip_api.downloader import Download

__all__ = ["CoroCallCounter", "AsyncTaskCache", "AsyncTimedTaskCache", "DownloadTaskCache", "cached_task",
           "cached_download"]

async_cache_log = logging.getLogger("studip_fuse.async_cache")


class DecoratorClass(object):
    def __init__(self, user_func):
        functools.update_wrapper(self, user_func)
        assert self.__wrapped__ == user_func
        # the outermost wrapper will always be generated last, so it will be the last to (over)write its cell in the dict
        self.__class__.DECORATORS[self.__class__.name_of_function(user_func)] = self

    def __getattr__(self, item):
        # redirect to __wrapped__ if an attr is not found on self
        return getattr(self.__wrapped__, item)

    def __get__(self, obj, type=None):
        # see https://stackoverflow.com/questions/47433768#comment81822552_47433786
        return functools.partial(self, obj)

    def __str__(self):
        return "<%s %s>" % (self.__class__.__name__, self.__wrapped__)

    def get_statistics(self):
        return {}

    # Class-Methods ####################################################################################################

    DECORATORS = {}  # type: Dict[str, DecoratorClass]

    @staticmethod
    def name_of_function(func) -> str:
        return func.__module__ + "." + getattr(func, "__qualname__", getattr(func, "__name__"))

    @classmethod
    def format_all_statistics(cls, decorators=None):
        if not decorators:
            decorators = cls.DECORATORS.values()
        lines = []
        for deco in decorators:
            lines.append({
                "decorator": str(deco),
                # "class": fullname.replace(deco.__name__, ""),
                # "name": deco.__name__,
                **deco.get_statistics()
            })
        return tabulate(lines, headers="keys")


class CoroCallCounter(DecoratorClass):
    def __init__(self, user_func):
        super().__init__(user_func)

        self.__call_counter = 0
        self.__successful_calls = 0
        self.__failed_calls = 0

    def get_statistics(self):
        return {
            "call_counter": self.__call_counter,
            "pending_calls": (self.__call_counter - self.__successful_calls - self.__failed_calls),
            "successful_calls": self.__successful_calls,
            "failed_calls": self.__failed_calls,
            **super().get_statistics()
        }

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


class AsyncTaskCache(DecoratorClass):
    class __Sentinel(object):
        def __str__(self):
            return "<AsyncTaskCache.CACHE_SENTINEL>"

    CACHE_SENTINEL = __Sentinel()
    LAST_CACHE_CLEAR = datetime.now()

    @classmethod
    async def clear_all_caches(cls):
        async_cache_log.warning("Clearing caches...")
        caches = [deco for deco in cls.DECORATORS.values() if isinstance(deco, DecoratorClass)]
        msg = "Clearing cache of %s wrapped functions. Last clear was %s s ago at %s.\n%s\n" % \
              (len(caches), datetime.now() - cls.LAST_CACHE_CLEAR, cls.LAST_CACHE_CLEAR,
               cls.format_all_statistics(caches))

        for cache in caches:
            await cache.clear_cache()

        cls.LAST_CACHE_CLEAR = datetime.now()
        msg = msg.strip()
        async_cache_log.info(msg)
        return msg

    def __init__(self, user_func):
        super().__init__(user_func)

        self.__hits = 0
        self.__misses = 0

        self.__cache = {}  # type: Dict[Any, asyncio.Future]

    @cached_property
    def __lock(self):
        # initialize lazy, so that asyncio.get_event_loop() doesn't create a new event loop before the actual one is set
        return asyncio.Lock()

    def get_statistics(self):
        return {
            "cache_hits": self.__hits,
            "cache_misses": self.__misses,
            "cache_size": len(self.__cache),
            **super().get_statistics()
        }

    async def clear_cache(self):
        async with self.__lock:
            self.__cache.clear()
            self.__hits = self.__misses = 0

    def __call__(self, *args, **kwargs):
        return self._get_cached_task(self._make_key(args, kwargs), args, kwargs)

    def _get_valid_cache_value(self, key, **kwargs):
        val = self.__cache.get(key, None)
        if self._is_valid_cache_value(key, val, **kwargs):
            return val
        else:
            return self.CACHE_SENTINEL

    def _make_key(self, args, kwargs):
        from functools import _make_key as make_key
        # FIXME calling with args or just one simple type might lead to different key than calling with kwargs
        return make_key(args, kwargs, typed=False)

    def _is_valid_cache_value(self, key, val, **kwargs):
        assert not kwargs, "Didn't expect kwargs %s" % kwargs
        if asyncio.isfuture(val):
            if not val.done():
                # use future result
                return True
            elif val.cancelled() or val.exception():
                # don't reuse failed tasks
                return False
            else:
                # reuse result of successful tasks
                return True
        elif val is self.CACHE_SENTINEL:
            return False
        else:
            assert val is None, (
                    "Expected result of invocation of user function %s to be a Task, but got '%s' of type %s" %
                    (self.__wrapped__, val, val.__class__))
            return False

    def _create_new_cache_value(self, key, old_value, args, kwargs):
        return asyncio.ensure_future(self.__wrapped__(*args, **kwargs))

    async def _get_cached_task(self, key, args, kwargs):
        res = self._get_valid_cache_value(key)
        if res is not self.CACHE_SENTINEL:
            self.__hits += 1
            return await res

        async with self.__lock:
            res = self._get_valid_cache_value(key)
            if res is not self.CACHE_SENTINEL:
                self.__hits += 1
                return await res

            res = self._create_new_cache_value(key, res, args, kwargs)
            self.__cache[key] = res
            self.__misses += 1
            return await res


class AsyncTimedTaskCache(AsyncTaskCache):
    def __init__(self, user_func):
        super().__init__(user_func)
        self.__cache_times = {}  # type: Dict[Any, int]
        self.__cache_fallbacks = {}  # type: Dict[Any, asyncio.Future]
        self.cache_timeout = 600  # type: int

    def get_statistics(self):
        return {
            "fallback_cache_size": len(self.__cache_fallbacks),
            # further stats could be: average / max cache age, fallback / active cache intersection, ...
            **super().get_statistics()
        }

    def _set_fallback_value(self, key, value, overwrite=True):
        if overwrite or self.__cache_fallbacks.get(key, self.CACHE_SENTINEL) is self.CACHE_SENTINEL:
            assert self._is_valid_cache_value(key, value, ignore_timeout=True)
            self.__cache_fallbacks[key] = value

    def _get_fallback_value(self, key):
        return self.__cache_fallbacks.get(key, self.CACHE_SENTINEL)

    def _get_any_value(self, key):
        res = self._get_valid_cache_value(key, ignore_timeout=True)
        if res is not self.CACHE_SENTINEL:
            return res
        return self.__cache_fallbacks.get(key, self.CACHE_SENTINEL)

    def _create_new_cache_value(self, key, old_value, args, kwargs):
        if self._is_valid_cache_value(key, old_value, ignore_timeout=True):
            self.__cache_fallbacks[key] = old_value
        value = super()._create_new_cache_value(key, old_value, args, kwargs)
        self.__cache_times[key] = time()
        return value

    def _is_valid_cache_value(self, key, val, ignore_timeout=False, **kwargs):
        if super()._is_valid_cache_value(key, val, **kwargs):
            return ignore_timeout or self.__cache_times[key] - time() < self.cache_timeout
        else:
            return False


class DownloadTaskCache(AsyncTaskCache):
    def _is_valid_cache_value(self, key, value, **kwargs):
        is_valid = super()._is_valid_cache_value(key, value, **kwargs)
        if is_valid and value.done() and isinstance(value.result(), Download):
            return super()._is_valid_cache_value(key, value.result().completed, **kwargs)
        else:
            return is_valid  # not a Download or still in progress, rely on result of super method

    def _create_new_cache_value(self, key, old_value, args, kwargs):
        if old_value is not self.CACHE_SENTINEL:
            assert asyncio.isfuture(old_value) and old_value.done() \
                   and not old_value.exception() and not old_value.cancelled(), \
                "Can't create a new cached task when old task is in invalid state: %s" % old_value
            assert isinstance(old_value.result(), Download), \
                "Expected result of old cached task to be a Download, but it was %s" % old_value.result()
            return asyncio.ensure_future(old_value.result().fork())

        return super()._create_new_cache_value(key, old_value, args, kwargs)


def cached_task(cache_class=AsyncTimedTaskCache):
    def wrapper(user_func):
        async_cache_log.debug(
            "Scheduling future execution of coroutine (result of calling) %s and caching successful executions in %s",
            user_func, cache_class)
        wrapped = CoroCallCounter(user_func)
        wrapped = cache_class(wrapped)
        return wrapped

    return wrapper


def cached_download():
    return cached_task(DownloadTaskCache)

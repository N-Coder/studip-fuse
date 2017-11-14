import asyncio
import functools
import logging

async_cache_log = logging.getLogger("studip_fuse.async_cache")

call_counter = 0


def schedule_task(schedule_with=asyncio.ensure_future):
    def wrapper(func):
        async_cache_log.debug("Scheduling future execution of coroutine / result of calling %s with %s",
                              func, schedule_with)

        async def awrapped(my_call_counter, *args):
            async_cache_log.debug("Started execution of #%s", my_call_counter)
            result = await func(*args)
            async_cache_log.debug("Completed execution of #%s = %s", my_call_counter, result)
            return result

        def wrapped(*args):
            global call_counter
            call_counter += 1
            my_call_counter = call_counter
            async_cache_log.debug("Scheduling #%s: %s(%s%s)",
                                  my_call_counter, schedule_with.__name__, func.__name__, args)
            future = schedule_with(awrapped(my_call_counter, *args))
            # async_cache_log.debug("Scheduled #%s as %s", my_call_counter, future)
            return future

        functools.update_wrapper(wrapped, func)
        return wrapped

    return wrapper

import asyncio
import concurrent.futures
import logging
from concurrent.futures import CancelledError
from contextlib import ExitStack, contextmanager

from studip_fuse.cache import CachedStudIPSession

log = logging.getLogger("studip_fuse.event_loop")


def main_loop(args, http_args, future: concurrent.futures.Future):
    with ExitStack() as stack:
        future = stack.enter_context(future_context(future))
        future.check_cancelled()
        loop = stack.enter_context(loop_context(args))
        future.check_cancelled()
        session = stack.enter_context(session_context(args, http_args, loop, future))
        future.check_cancelled()

        log.info("Loop and session ready, sending result back to main thread")
        future.set_result((loop, session))

        log.info("Running asyncio event loop...")
        loop.run_forever()


@contextmanager
def future_context(future):
    def check_cancelled():
        if future.cancelled():
            raise CancelledError()

    future.check_cancelled = check_cancelled
    try:
        yield future
    except Exception as e:
        if not future.done():
            future.set_exception(e)
    finally:
        if not future.done():
            msg = "Event loop thread did not report result back to main thread."
            log.warning(msg)
            future.set_exception(RuntimeError(msg))


@contextmanager
def loop_context(args):
    log.info("Initializing asyncio event loop...")
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    if args.debug:
        loop.set_debug(True)
    try:
        yield loop
    finally:
        async def shutdown_loop_async(loop):
            log.debug("Draining loop")
            await asyncio.sleep(1)
            await loop.shutdown_asyncgens()
            log.debug("Loop drained")

        loop.run_until_complete(shutdown_loop_async(loop))
        log.info("Event loop closed")


@contextmanager
def session_context(args, http_args, loop, future: concurrent.futures.Future):
    log.info("Opening StudIP session...")
    password = getpass(args)

    session = CachedStudIPSession(
        loop=loop, studip_base=args.studip, sso_base=args.sso, cache_dir=args.cache, http_args=http_args)
    try:
        coro = session.do_login(user_name=args.user, password=password.strip())
        task = asyncio.ensure_future(coro, loop=loop)
        future.add_done_callback(lambda f: task.cancel() if f.cancelled() else None)

        future.check_cancelled()
        loop.run_until_complete(task)

        yield session
    finally:
        async def shutdown_session_async(session):
            log.debug("Closing session")
            await session.close()
            log.debug("Session closed")

        log.info("Initiating shut down sequence...")
        loop.run_until_complete(shutdown_session_async(loop))

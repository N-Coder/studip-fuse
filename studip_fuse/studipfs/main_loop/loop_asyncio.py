import asyncio
import concurrent.futures
import inspect
import logging
from asyncio import AbstractEventLoop
from concurrent.futures import CancelledError
from contextlib import ExitStack, contextmanager

from studip_fuse.aioutils.asyncio_impl.filestore import AsyncioFileStore as AFileStore
from studip_fuse.aioutils.asyncio_impl.http import AsyncioSyncRequestsCachedHTTPSession as AHTTPSession
from studip_fuse.aioutils.asyncio_impl.pipeline import AsyncioPipeline
from studip_fuse.avfs.real_path import RealPath
from studip_fuse.studipfs.api.session import StudIPSession
from studip_fuse.studipfs.fuse_ops import LoopSetupResult
from studip_fuse.studipfs.path.studip_path import StudIPPath

log = logging.getLogger(__name__)


def setup_asyncio_loop(args):
    def start(future: concurrent.futures.Future):
        with ExitStack() as stack:
            future = stack.enter_context(future_context(future))
            check_cancelled(future)
            loop = stack.enter_context(loop_context(args))  # type: AbstractEventLoop
            check_cancelled(future)
            root_rp = stack.enter_context(session_context(args, loop, future))
            check_cancelled(future)

            log.info("Loop and session ready, sending result back to main thread")

            def async_result(corofn, *args, **kwargs):
                assert not inspect.iscoroutine(corofn)
                assert inspect.iscoroutinefunction(corofn)
                if not loop.is_running():
                    log.warning("Submitting coroutinefunction %s to paused main asyncio loop %s, this shouldn't happen",
                                corofn, loop)
                return asyncio.run_coroutine_threadsafe(corofn(*args, **kwargs), loop).result()

            future.set_result(LoopSetupResult(
                loop_stop_fn=lambda: loop.call_soon_threadsafe(loop.stop),
                loop_run_fn=async_result,
                root_rp=root_rp))

            log.info("Running asyncio event loop...")
            loop.run_forever()

    return start


def check_cancelled(future):
    if future.cancelled():
        raise CancelledError()


@contextmanager
def future_context(future):
    try:
        yield future
    except Exception as e:
        if not future.done():
            future.set_exception(e)
        else:
            raise
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
    if args.debug_aio:
        loop.set_debug(True)
    try:
        yield loop
    finally:
        async def drain_loop_async(loop):
            log.debug("Draining loop")
            # loop.stop will already have been called, otherwise run_forever wouldn't have terminated
            await asyncio.sleep(1)
            try:
                await loop.shutdown_asyncgens()
            except AttributeError:
                pass  # shutdown_asyncgens was added in 3.6
            log.debug("Loop drained")

        loop.run_until_complete(drain_loop_async(loop))
        loop.close()
        log.info("Event loop closed")


@contextmanager
def session_context(args, loop, future: concurrent.futures.Future):
    log.info("Opening StudIP session...")

    # TODO make mockable
    http = AHTTPSession()
    storage = AFileStore(http=http, location=args.cache)
    session = StudIPSession(studip_base=args.studip, http=http, storage=storage)
    try:
        coro = session.do_login(username=args.user, password=args.get_password())
        task = asyncio.ensure_future(coro, loop=loop)
        future.add_done_callback(lambda f: task.cancel() if f.cancelled() else None)

        check_cancelled(future)
        loop.run_until_complete(task)
        # loop.run_until_complete(session.load_model(update=True))

        root_vp = StudIPPath(parent=None, path_segments=[], known_data={}, next_path_segments=args.format.split("/"),
                             session=session, pipeline_type=AsyncioPipeline)
        root_rp = RealPath(parent=None, generating_vps={root_vp})
        yield root_rp
    finally:
        async def shutdown_session_async(session):
            log.debug("Closing session")
            await session.close()
            await storage.close()
            await http.close()
            log.debug("Session closed")

        log.info("Initiating shut down sequence...")
        loop.run_until_complete(shutdown_session_async(session))

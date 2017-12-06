import asyncio
import concurrent.futures
import functools
import logging
import os
import sys
import warnings
from getpass import getpass
from threading import Thread

import sh
from fuse import FUSE, fuse_get_context

from studip_fuse.cached_session import CachedStudIPSession
from studip_fuse.cmd_util import dump_loop_stack, format_stack, parse_args, thread_log
from studip_fuse.fs_driver import FUSEView, LoggingFUSEView
from studip_fuse.real_path import RealPath
from studip_fuse.virtual_path import VirtualPath

logging.basicConfig(level=logging.INFO)


def main():
    args, http_args, fuse_args = parse_args()
    if args.debug:
        logging.root.setLevel(logging.DEBUG)
        warnings.resetwarnings()
    else:
        logging.getLogger("sh").setLevel(logging.WARNING)
        logging.getLogger("asyncio").setLevel(logging.WARNING)

    future = concurrent.futures.Future()
    loop_thread = Thread(target=functools.partial(run_loop, args, http_args, future), name="aio event loop",
                         daemon=True)
    loop_thread.start()
    loop, session = None, None
    try:
        logging.debug("Loop thread started, waiting for session initialization")
        loop, session = future.result()

        run_fuse(loop, session, args, fuse_args)
    except:
        logging.error("FUSE driver crashed", exc_info=True)
    finally:
        logging.info("FUSE driver stopped, also stopping event loop")
        future.cancel()
        if loop:
            loop.stop()

        await_loop_thread_shutdown(loop, loop_thread)


def run_loop(args, http_args, future: concurrent.futures.Future):
    try:
        logging.info("Initializing asyncio event loop...")
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        if args.debug:
            loop.set_debug(True)
    except Exception as e:
        logging.debug("Loop initialization failed, propagating result back to main thread")
        future.set_exception(e)
        raise

    session = None
    try:
        if future.cancelled():
            return

        logging.info("Opening StudIP session")
        if args.pwfile == "-":
            password = getpass()
        else:
            try:
                with open(args.pwfile) as f:
                    password = f.read()
            except FileNotFoundError as e:
                logging.warning("%s. Either specifiy a file from which your Stud.IP password can be read "
                                "or use `--pwfile -` to enter it using a promt in the shell." % e)
                future.set_exception(e)
                return
        coro = CachedStudIPSession(
            user_name=args.user, password=password.strip(),
            studip_base=args.studip, sso_base=args.sso,
            cache_dir=args.cache, http_args=http_args
        ).__aenter__()
        password = ""
        task = asyncio.ensure_future(coro, loop=loop)
        future.add_done_callback(lambda f: task.cancel() if f.cancelled() else None)
        if future.cancelled():
            return
        session = loop.run_until_complete(task)
        if future.cancelled():
            return

        logging.debug("Loop and session ready, sending result back to main thread")
        future.set_result((loop, session))

        logging.info("Running asyncio event loop...")
        loop.run_forever()

    except Exception as e:
        if not future.done():
            future.set_exception(e)

    finally:
        logging.info("asyncio event loop stopped, cleaning up and closing")

        if not future.done():
            msg = "Event loop thread did not report result back to main thread."
            logging.warning(msg)
            future.set_exception(RuntimeError(msg))

        try:
            if session:
                loop.run_until_complete(shutdown_loop_async(loop, session))
                logging.info("Cleaned up session, closing event loop")
        except:
            logging.warning("Clean-up failed, closing", exc_info=True)

        loop.close()
        logging.info("Event loop closed, shutdown complete")


async def shutdown_loop_async(loop, session):
    await session.__aexit__(*sys.exc_info())
    logging.debug("Session closed")
    await asyncio.sleep(1)
    await loop.shutdown_asyncgens()
    logging.debug("Loop drained")


def await_loop_thread_shutdown(loop, loop_thread):
    # print loop stack trace until the loop thread completed
    counter = 0
    while loop_thread.is_alive() and counter < 6:
        loop_thread.join(5)
        counter += 1
        if loop_thread.is_alive():
            if loop:
                dump_loop_stack(loop)
            else:
                thread_log.info("Waiting for loop thread to abort initialization...")
                thread_log.debug("Thread stack trace:\n %s", format_stack(sys._current_frames()[loop_thread.ident]))

    if loop_thread.is_alive():
        logging.warning("Shutting down main thread and thus killing hung event loop daemon thread")


def run_fuse(loop, session, args, fuse_args):
    logging.info("Initializing virtual file system")
    vp = VirtualPath(session=session, path_segments=[], known_data={}, parent=None,
                     next_path_segments=args.format.split("/"))
    rp = RealPath(parent=None, generating_vps={vp})

    os.makedirs(args.cache, exist_ok=True)
    try:
        os.makedirs(args.mount, exist_ok=True)
    except FileExistsError:  # if mountpoint was not unmounted properly
        pass
    try:
        sh.fusermount("-u", args.mount)
    except sh.ErrorReturnCode as e:
        if "entry for" not in str(e) or "not found in" not in str(e):
            logging.warning("Could not unmount mount path %s", args.mount, exc_info=True)
        else:
            # print last line of stderr output, containing the entry not found message
            logging.debug(e.stderr.decode("UTF-8", "replace").strip().split("\n")[-1])
    except sh.CommandNotFound as e:
        logging.info("Could not unmount mountpoint before mounting, because fusermount is not available")

    logging.debug("Initialization done, handing over to FUSE driver to mount at %s (uid=%s, gid=%s, pid=%s)",
                  args.mount, *fuse_get_context())
    if args.debug:
        fuse_ops = LoggingFUSEView(rp, loop)
    else:
        fuse_ops = FUSEView(rp, loop)
    logging.info("Ready")
    FUSE(fuse_ops, args.mount, **fuse_args)


if __name__ == "__main__":
    main()

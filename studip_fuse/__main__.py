import argparse as argparse
import asyncio
import concurrent.futures
import functools
import logging
import os
import sys
import threading
import traceback
import warnings
from getpass import getpass
from threading import Thread

import sh
from fuse import FUSE
from more_itertools import one

from studip_fuse.cached_session import CachedStudIPSession
from studip_fuse.fs_driver import FUSEView, LoggingFUSEView
from studip_fuse.real_path import RealPath
from studip_fuse.virtual_path import VirtualPath

logging.basicConfig(level=logging.INFO)
thread_log = logging.getLogger("threads")


def main():
    args = parse_args()
    if args.debug:
        logging.root.setLevel(logging.DEBUG)
        warnings.resetwarnings()
    else:
        logging.getLogger("sh").setLevel(logging.WARNING)
        logging.getLogger("asyncio").setLevel(logging.WARNING)

    future = concurrent.futures.Future()
    loop_thread = Thread(target=functools.partial(run_loop, args, future), name="aio event loop")
    loop_thread.start()
    logging.debug("Loop thread started, waiting for session initialization")
    loop, session = future.result()
    try:
        run_fuse(loop, session, args)
    except:
        logging.error("FUSE driver crashed", exc_info=True)
    finally:
        logging.info("FUSE driver stopped, also stopping event loop")
        loop.stop()

        if args.debug:
            # print loop stack trace until the loop thread completed
            loop_thread.join(10)
            while loop_thread.is_alive():
                dump_loop_stack(loop)
                loop_thread.join(5)


def parse_args():
    from studip_fuse import __version__ as prog_version
    mkpath = lambda p: os.path.realpath(os.path.expanduser(p))
    parser = argparse.ArgumentParser(description='Stud.IP Fuse')
    parser.add_argument('--user', help='username', required=True)
    parser.add_argument('--pwfile', help='password file', default=mkpath('~/.studip-pw'))
    parser.add_argument('--format', help='path format',
                        default="{semester-lexical-short}/{course}/{type}/{short-path}/{name}")
    parser.add_argument('--mount', help='mount path', default=mkpath("~/studip/mount"))
    parser.add_argument('--cache', help='cache oath', default=mkpath("~/studip/cache"))  # TODO use XDG_CACHE_DIR
    parser.add_argument('--studip', help='Stud.IP base URL', default="https://studip.uni-passau.de")
    parser.add_argument('--sso', help='SSO base URL', default="https://sso.uni-passau.de")
    parser.add_argument('--debug', help='enable debug mode', action='store_true')
    parser.add_argument('--allowroot', help='allow root to access the mounted directory (required for overlayFS)',
                        action='store_true')
    parser.add_argument('--version', action='version', version="%(prog)s " + prog_version)
    args = parser.parse_args()
    return args


def run_loop(args, future: concurrent.futures.Future):
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

    try:
        try:
            logging.info("Opening StudIP session")
            if args.pwfile == "-":
                password = getpass()
            else:
                with open(args.pwfile) as f:
                    password = f.read()
            coro = CachedStudIPSession(
                user_name=args.user, password=password.strip(),
                studip_base=args.studip, sso_base=args.sso,
                cache_dir=args.cache
            ).__aenter__()
            password = ""
            session = loop.run_until_complete(coro)
        except Exception as e:
            logging.debug("Session initialization failed, propagating result back to main thread")
            future.set_exception(e)
            raise

        logging.debug("Loop and session ready, sending result back to main thread")
        future.set_result((loop, session))

        try:
            logging.info("Running asyncio event loop...")
            loop.run_forever()
        finally:
            logging.info("asyncio event loop stopped, cleaning up")
            try:
                loop.run_until_complete(shutdown_loop_async(loop, session))
                logging.info("Cleaned up, closing event loop")
            except:
                logging.warning("Clean-up failed, closing", exc_info=True)

    finally:
        loop.close()
        logging.info("Event loop closed, shutdown complete")


async def shutdown_loop_async(loop, session):
    await session.__aexit__(*sys.exc_info())
    logging.debug("Session closed")
    await asyncio.sleep(1)
    await loop.shutdown_asyncgens()
    logging.debug("Loop drained")


def run_fuse(loop, session, args):
    logging.info("Initializing virtual file system")
    vp = VirtualPath(session=session, path_segments=[], known_data={}, parent=None,
                     next_path_segments=args.format.split("/"))
    rp = RealPath(parent=None, generating_vps={vp})

    try:
        sh.fusermount("-u", args.mount)
    except sh.ErrorReturnCode as e:
        if "entry for" not in str(e) or "not found in" not in str(e):
            logging.warning("Could not unmount mount path %s", args.mount, exc_info=True)
        else:
            logging.debug(e.stderr.decode("UTF-8", "replace").strip().split("\n")[-1])
    os.makedirs(args.mount, exist_ok=True)
    os.makedirs(args.cache, exist_ok=True)

    logging.debug("Initialization done, handing over to FUSE driver")
    if args.debug:
        fuse_ops = LoggingFUSEView(rp, loop)
    else:
        fuse_ops = FUSEView(rp, loop)
    logging.info("Ready")
    FUSE(fuse_ops, args.mount, foreground=True, allow_root=args.allowroot, debug=args.debug)


def dump_loop_stack(loop):
    format_stack = lambda stack: "".join(traceback.format_stack(stack[0] if isinstance(stack, list) else stack))
    current_task = asyncio.Task.current_task(loop=loop)
    pending_tasks = [t for t in asyncio.Task.all_tasks(loop=loop) if not t.done() and t is not current_task]
    loop_thread = one(t for t in threading.enumerate() if t.ident == loop._thread_id)
    thread_log.debug("Current task %s in loop %s in loop thread %s", current_task, loop, loop_thread)
    if current_task:
        thread_log.debug("Task stack trace:\n %s", format_stack(current_task.get_stack()))
    thread_log.debug("Thread stack trace:\n %s", format_stack(sys._current_frames()[loop_thread.ident]))
    pending_tasks_str = "\n".join(
        str(t) + "\n" + format_stack(t.get_stack())
        for t in pending_tasks)
    thread_log.debug("%s further pending tasks:\n %s", len(pending_tasks), pending_tasks_str)


if __name__ == "__main__":
    main()

import logging

logging.basicConfig(level=logging.INFO)

import argparse as argparse
import asyncio
import functools
import os
import sys
from threading import Thread
import warnings

import attr
import sh
from fuse import FUSE

from studip_api.session import StudIPSession
from studip_fuse.async_cache import schedule_task
from studip_fuse.fs_driver import FUSEView
from studip_fuse.virtual_path import VirtualPath
from studip_fuse.real_path import RealPath


@attr.s(hash=False)
class CachedStudIPSession(StudIPSession):
    cache_dir: str = attr.ib()

    @functools.lru_cache()
    @schedule_task()
    async def get_semesters(self):
        return await super().get_semesters()

    @functools.lru_cache()
    @schedule_task()
    async def get_courses(self, semester):
        return await super().get_courses(semester)

    @functools.lru_cache()
    @schedule_task()
    async def get_course_files(self, course):
        return await super().get_course_files(course)

    @functools.lru_cache()
    @schedule_task()
    async def get_folder_files(self, folder):
        return await super().get_folder_files(folder)

    @functools.lru_cache()
    @schedule_task()
    async def download_file_contents(self, file, dest=None, chunk_size=1024 * 256):
        # TODO check integrity of existing paths and reuse them
        if not dest:
            dest = os.path.join(self.cache_dir, file.id)
        return await super().download_file_contents(file, dest)


async def shutdown_loop(loop, session):
    await session.__aexit__(*sys.exc_info())
    logging.debug("Session closed")
    await asyncio.sleep(1)
    await loop.shutdown_asyncgens()
    logging.debug("Loop drained")


def main():
    from studip_fuse import __version__ as prog_version

    mkpath = lambda p: os.path.realpath(os.path.expanduser(p))
    parser = argparse.ArgumentParser(description='Stud.IP Fuse')
    parser.add_argument('--user', help='username', required=True)
    parser.add_argument('--pwfile', help='password file', default=mkpath('~/.studip-pw'))
    parser.add_argument('--format', help='path format',
                        default="{semester-lexical-short}/{course}/{type}/{short-path}/{name}")
    parser.add_argument('--mount', help='mount path', default=mkpath("~/studip/mount"))
    parser.add_argument('--cache', help='cache oath', default=mkpath("~/studip/cache"))
    parser.add_argument('--studip', help='Stud.IP base URL', default="https://studip.uni-passau.de")
    parser.add_argument('--sso', help='SSO base URL', default="https://sso.uni-passau.de")
    parser.add_argument('--debug', help='enable debug mode', action='store_true')
    parser.add_argument('--version', action='version', version="%(prog)s " + prog_version)
    args = parser.parse_args()

    loop = asyncio.get_event_loop()

    if args.debug:
        loop.set_debug(True)
        logging.root.setLevel(logging.DEBUG)
        warnings.resetwarnings()
    else:
        logging.getLogger("sh").setLevel(logging.WARNING)

    loop_thread = Thread(target=loop.run_forever, name="aio event loop")
    loop_thread.start()

    logging.info("Opening StudIP session")
    with open(args.pwfile) as f:
        password = f.read()
    session = asyncio.run_coroutine_threadsafe(CachedStudIPSession(
        user_name=args.user,
        password=password,
        studip_base=args.studip,
        sso_base=args.sso,
        cache_dir=args.cache
    ).__aenter__(), loop).result()
    password = ""

    try:
        logging.info("Initializing virtual file system")
        vp = VirtualPath(session=session, path_segments=[], known_data={}, parent=None,
                         next_path_segments=args.format.split("/"))
        rp = RealPath(parent=None, generating_vps={vp})

        try:
            sh.fusermount("-u", args.mount)
        except sh.ErrorReturnCode as e:
            if "entry for %s not found in" % args.mount not in str(e):
                logging.warning("Could not unmount mount path %s", args.mount, exc_info=True)
            else:
                logging.debug(e.stderr.decode("UTF-8", "replace").strip().split("\n")[-1])
        os.makedirs(args.mount, exist_ok=True)
        os.makedirs(args.cache, exist_ok=True)

        logging.info("Handing over to FUSE driver")  # TODO offline support?
        fuse_ops = FUSEView(rp, loop)
        FUSE(fuse_ops, args.mount, foreground=True, allow_root=True, debug=args.debug)
    except:
        logging.error("FUSE driver interrupted", exc_info=True)
    finally:
        logging.info("Shutting down")
        loop.stop()
        logging.debug("Interrupted loop thread, waiting for join")
        loop_thread.join(timeout=5)
        while loop_thread.is_alive():
            logging.warning("Waiting for loop thread interrupt...")
            loop_thread.join(timeout=5)
        logging.debug("Taking over event loop and draining")
        loop.run_until_complete(shutdown_loop(loop, session))
        logging.debug("Event loop drained, closing")
        loop.close()
        logging.info("Event loop closed")


if __name__ == "__main__":
    main()

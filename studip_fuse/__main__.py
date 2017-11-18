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
        if not dest:
            dest = os.path.join(self.cache_dir, file.id)
        return await super().download_file_contents(file, dest)


async def shutdown_loop(loop, session):
    await session.__aexit__(*sys.exc_info())
    logging.debug("Session closed")
    await asyncio.sleep(1)
    await loop.shutdown_asyncgens()
    logging.debug("Loop drained")


if __name__ == "__main__":
    mount_path = os.path.realpath(os.path.expanduser("~/studip/mount"))
    cache_path = os.path.realpath(os.path.expanduser("~/studip/cache"))
    parser = argparse.ArgumentParser(description='Stud.IP Fuse')
    parser.add_argument('user', help='username')
    parser.add_argument('--debug', help='enable debug mode', action='store_true')
    args = parser.parse_args()

    loop = asyncio.get_event_loop()

    if args.debug:
        loop.set_debug(True)
        logging.root.setLevel(logging.DEBUG)
        warnings.resetwarnings()

    loop_thread = Thread(target=loop.run_forever, name="loop")
    loop_thread.start()

    logging.info("Opening StudIP session")
    with open(os.path.expanduser('~/.studip-pwd')) as f:
        password = f.read()
    session = asyncio.run_coroutine_threadsafe(CachedStudIPSession(
        user_name=args.user,
        password=password,
        studip_base="https://studip.uni-passau.de",
        sso_base="https://sso.uni-passau.de",
        cache_dir=cache_path
    ).__aenter__(), loop).result()
    password = ""

    try:
        logging.info("Initializing virtual file system")
        vp = VirtualPath(session=session, path_segments=[], known_data={}, parent=None,
                         next_path_segments="{semester-lexical-short}/{course}/{type}/{short-path}/{name}".split("/"))
        rp = RealPath(parent=None, generating_vps={vp})

        try:
            sh.fusermount("-u", mount_path)
        except sh.ErrorReturnCode as e:
            if "entry for %s not found in" % mount_path not in str(e):
                logging.warning("Could not unmount mount path %s", mount_path, exc_info=True)
            else:
                logging.debug(e.stderr.decode("UTF-8", "replace").strip().split("\n")[-1])
        os.makedirs(mount_path, exist_ok=True)
        os.makedirs(cache_path, exist_ok=True)

        logging.info("Handing over to FUSE driver")
        fuse_ops = FUSEView(rp)
        FUSE(fuse_ops, mount_path, nothreads=True, foreground=True)
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

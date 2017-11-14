import logging
from threading import Thread

logging.basicConfig(level=logging.INFO)

import argparse as argparse
import asyncio
import functools
import os
import sys
import warnings

import attr
import sh
from fuse import FUSE

from studip_api.session import StudIPSession
from studip_fuse.async_cache import schedule_task
from studip_fuse.fs_driver import FUSEView
from studip_fuse.virtual_path import RealPath, VirtualPath


@attr.s(hash=False)
class CachedStudIPSession(StudIPSession):
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


async def shutdown_loop(loop, session):
    await session.__aexit__(*sys.exc_info())
    await asyncio.sleep(1)
    await loop.shutdown_asyncgens()
    loop.stop()
    loop.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Stud.IP Fuse')
    parser.add_argument('user', help='username')
    parser.add_argument('debug', help='enable debug mode', action='store_true')
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
        sso_base="https://sso.uni-passau.de"
    ).__aenter__(), loop).result()
    password = ""

    try:
        vp = VirtualPath(session=session, path_segments=[], known_data={}, parent=None,
                         next_path_segments="{semester-lexical-short}/{course}/{type}/{short-path}/{name}".split("/"))
        rp = RealPath(parent=None, generating_vps={vp})

        logging.info("Starting FUSE driver")
        mount_path = os.path.realpath(os.path.expanduser("~/studip/mount"))
        cache_path = os.path.realpath(os.path.expanduser("~/studip/cache"))

        try:
            os.makedirs(mount_path, exist_ok=True)
            os.makedirs(cache_path, exist_ok=True)
            sh.fusermount("-u", mount_path)
        except:
            pass

        fuse_ops = FUSEView(rp)
        FUSE(fuse_ops, mount_path, nothreads=True, foreground=True)
    finally:
        logging.info("Shutting down")
        asyncio.run_coroutine_threadsafe(shutdown_loop(loop, session), loop)
        loop_thread.join()

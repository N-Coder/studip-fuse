import logging

logging.basicConfig(level=logging.DEBUG)

import asyncio
import os
import warnings

from studip_fuse.async_fetch import main
from studip_fuse.fs_driver import FUSEView
from studip_fuse.virtual_path import RealPath, VirtualPath


def async_fetch():
    loop = asyncio.get_event_loop()
    loop.set_debug(True)
    logging.basicConfig(level=logging.DEBUG)
    logging.getLogger("asyncio").setLevel(logging.INFO)
    warnings.resetwarnings()

    session = loop.run_until_complete(loop.create_task(main()))

    pending_tasks = [task for task in asyncio.Task.all_tasks() if not task.done()]
    while pending_tasks:
        logging.warning("%s uncompleted tasks", len(pending_tasks))
        logging.warning("Uncompleted tasks are: %s", pending_tasks)
        try:
            loop.run_until_complete(asyncio.gather(*pending_tasks))
        except:
            logging.warning("Uncompleted task raised an exception", exc_info=True)
        pending_tasks = [task for task in asyncio.Task.all_tasks() if not task.done()]

    loop.run_until_complete(asyncio.sleep(1))
    loop.close()

    return session


def run_fuse(rp):
    from fuse import FUSE
    import sh

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


def print_vp_tree(vp):
    it = iter(vp)
    try:
        level, sub_path, is_folder, sub_vps = next(it)
        while True:
            print("\t" * level + sub_path + (" [folder]" if is_folder else " [file]") +
                  (" *%s" % len(sub_vps) if len(sub_vps) > 1 else ""))
            level, sub_path, is_folder, sub_vps = it.send(True)
    except StopIteration:
        pass


if __name__ == "__main__":
    session = async_fetch()
    vp = VirtualPath(session=session, path_segments=[], known_data={}, parent=None,
                     next_path_segments="{semester-lexical-short}/{course}/{type}/{short-path}/{name}".split("/"))
    rp = RealPath(parent=None, generating_vps={vp})
    # print_vp_tree(vp)
    print("Starting FUSE")
    run_fuse(rp)

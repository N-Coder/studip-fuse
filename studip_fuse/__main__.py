import asyncio
import logging
import os
import warnings
from asyncio.futures import _chain_future as chain_future

from studip_fuse.async_fetch import AsyncState, main
from studip_fuse.fs_driver import FUSEView
from studip_fuse.virtual_path import VirtualPath


def async_fetch():
    loop = asyncio.get_event_loop()
    loop.set_debug(True)
    logging.basicConfig(level=logging.DEBUG)
    warnings.resetwarnings()

    state = AsyncState()
    chain_future(loop.create_task(main(state)), state.root)
    loop.run_until_complete(state.root)

    loop.run_until_complete(asyncio.sleep(1))
    loop.close()

    return state


def run_fuse(vp):
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

    fuse_ops = FUSEView(vp)
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


if __name__=="__main__":
    state = async_fetch()
    vp = VirtualPath(state=state, session=None, path_segments=[], known_data={}, parent=None,
                     next_path_segments="{semester-lexical-short}/{course}/{type}/{short-path}/{name}".split("/"))
    # print_vp_tree(vp)
    print("Starting FUSE")
    run_fuse(vp)

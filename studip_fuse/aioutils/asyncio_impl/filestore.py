import attr

from studip_fuse.aioutils.interface import FileStore


@attr.s()
class AsyncioFileStore(FileStore):
    pass

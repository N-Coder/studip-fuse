import errno
import logging
import os

import attr
from fuse import Operations

from studip_fuse.path_util import path_name
from studip_fuse.virtual_path import RealPath

log = logging.getLogger("studip_fs.fs_drive")


@attr.s(frozen=True)
class FUSEView(Operations):
    root_rp: RealPath = attr.ib()

    def _resolve(self, partial: str) -> RealPath:
        resolved_real_file = self.root_rp.resolve(partial)
        if not resolved_real_file:
            raise OSError(errno.ENOENT, "No such file or directory", partial)
        else:
            return resolved_real_file

    def readdir(self, path, fh):
        resolved_real_file = self._resolve(path)
        if resolved_real_file.is_folder:
            return ['.', '..'] + [path_name(rp.path) for rp in resolved_real_file.list_contents()]
        else:
            raise OSError(errno.ENOTDIR)

    def access(self, path, mode):
        return self._resolve(path).access(mode)

    def getattr(self, path, fh=None):
        return self._resolve(path).getattr()

    def open(self, path, flags):
        resolved_real_file = self._resolve(path)
        if resolved_real_file.is_folder:
            raise OSError(errno.EISDIR)
        else:
            return resolved_real_file.open_file(flags)

    def read(self, path, length, offset, fh):
        os.lseek(fh, offset, os.SEEK_SET)  # TODO make lazy
        return os.read(fh, length)

    def flush(self, path, fh):
        return os.fsync(fh)

    def release(self, path, fh):
        return os.close(fh)

    def fsync(self, path, fdatasync, fh):
        return self.flush(path, fh)

import errno
import functools
import logging
import os
from typing import List, Tuple

import attr
from fuse import Operations

from studip_api.util import mkdict
from studip_fuse.virtual_path import VirtualPath, iterate_vps_hierarchically

log = logging.getLogger("studip_fs.fs_drive")
resolve_log = log.getChild("resolve_path")
readdir_log = log.getChild("readdir")


@attr.s(frozen=True)
class FUSEView(Operations):
    root_vp: VirtualPath = attr.ib()

    @functools.lru_cache()
    def _resolve(self, partial: str) -> Tuple[int, str, bool, List[VirtualPath]]:
        partial = os.path.normpath(partial)
        while partial.startswith("/"):
            partial = partial[1:]
        while partial.endswith("/"):
            partial = partial[:-1]

        if partial == '':
            resolve_log.debug("Returning root path early")
            return -1, '', self.root_vp.is_folder, [self.root_vp]

        resolve_log.debug("Searching for '%s'", partial)
        it = iter(self.root_vp)
        level, sub_path, is_folder, sub_vps = (None,) * 4
        try:
            level, sub_path, is_folder, sub_vps = next(it)
            while True:
                substring = partial.startswith(sub_path)
                if substring or partial == sub_path:
                    resolve_log.debug("Found:\t" + "\t" * level + sub_path + (" [folder]" if is_folder else " [file]") +
                                      (" *%s" % len(sub_vps) if len(sub_vps) > 1 else ""))
                if partial == sub_path:
                    break
                else:
                    level, sub_path, is_folder, sub_vps = it.send(substring)
        except StopIteration:
            pass

        if sub_path == partial:
            resolve_log.debug("Returning '%s' == '%s'", partial, sub_path)
            return level, sub_path, is_folder, list(sub_vps)
        else:
            resolve_log.debug("No such file or directory %s! (not '%s'.startswith('%s'))", partial, sub_path, partial)
            # TODO also cache missing files
            raise OSError(errno.ENOENT, "No such file or directory", partial)

    def access(self, path, mode):
        level, sub_path, is_folder, sub_vps = self._resolve(path)
        for sub_vp in sub_vps:
            sub_vp.access(mode)

    def getattr(self, path, fh=None):
        level, sub_path, is_folder, sub_vps = self._resolve(path)
        st = mkdict(*(sub_vp.getattr() for sub_vp in sub_vps))
        return {key: val for key, val in st.items() if key in
                ('st_atime', 'st_ctime', 'st_gid', 'st_mode', 'st_mtime', 'st_nlink', 'st_size', 'st_uid')}

    @functools.lru_cache()
    def readdir(self, path, fh):
        level, sub_path, is_folder, sub_vps = self._resolve(path)
        if is_folder:
            contents = ['.', '..']
            it = iterate_vps_hierarchically(sub_vps)
            try:
                level, sub_path, is_folder, sub_vps = next(it)
                while True:
                    readdir_log.debug("%s: %s", path, sub_path)
                    contents.append(sub_path.split("/")[-1])
                    level, sub_path, is_folder, sub_vps = it.send(False)
            except StopIteration:
                pass
            return contents
        else:
            raise OSError(errno.ENOTDIR)

    def open(self, path, flags):
        level, sub_path, is_folder, sub_vps = self._resolve(path)
        if is_folder or len(sub_vps) != 1:
            raise OSError(errno.EISDIR)
        else:
            return sub_vps[0].open_file(flags)

    def read(self, path, length, offset, fh):
        os.lseek(fh, offset, os.SEEK_SET)  # TODO make lazy
        return os.read(fh, length)

    def flush(self, path, fh):
        return os.fsync(fh)

    def release(self, path, fh):
        return os.close(fh)

    def fsync(self, path, fdatasync, fh):
        return self.flush(path, fh)

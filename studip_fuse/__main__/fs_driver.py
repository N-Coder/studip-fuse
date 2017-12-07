import asyncio
import errno
import functools
import logging
import os
from typing import List

import attr
from fuse import LoggingMixIn, Operations

from studip_fuse.path import RealPath, path_name

log = logging.getLogger("studip_fuse.fs_drive")


@attr.s(frozen=True)
class FUSEView(Operations):
    root_rp: RealPath = attr.ib()
    loop = attr.ib(hash=False)

    def await_async(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result()

    @functools.lru_cache()  # TODO refactor multi-level caching, add ttl / SIGUSR-based clearing
    def _resolve(self, partial: str) -> RealPath:
        return self.await_async(self._aresolve(partial))

    async def _aresolve(self, partial: str) -> RealPath:
        resolved_real_file = await self.root_rp.resolve(partial)
        if not resolved_real_file:
            raise OSError(errno.ENOENT, "No such file or directory", partial)
        else:
            return resolved_real_file

    @functools.lru_cache()
    def readdir(self, path, fh) -> List[str]:
        async def _async() -> List[str]:
            resolved_real_file = await self._aresolve(path)
            if resolved_real_file.is_folder:
                return ['.', '..'] + [path_name(rp.path) for rp in await resolved_real_file.list_contents()]
            else:
                raise OSError(errno.ENOTDIR)

        return self.await_async(_async())

    def access(self, path, mode):
        return self._resolve(path).access(mode)

    def getattr(self, path, fh=None):
        return self._resolve(path).getattr()

    def open(self, path, flags):
        resolved_real_file = self._resolve(path)
        if resolved_real_file.is_folder:
            raise OSError(errno.EISDIR)
        else:
            return self.await_async(resolved_real_file.open_file(flags))

    def read(self, path, length, offset, fh):
        os.lseek(fh, offset, os.SEEK_SET)  # TODO make lazy
        return os.read(fh, length)

    def flush(self, path, fh):
        return os.fsync(fh)

    def release(self, path, fh):
        return os.close(fh)

    def fsync(self, path, fdatasync, fh):
        return self.flush(path, fh)


@attr.s(frozen=True)
class LoggingFUSEView(FUSEView, LoggingMixIn):
    pass

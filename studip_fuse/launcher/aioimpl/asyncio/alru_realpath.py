from concurrent.futures import CancelledError

from async_lru import alru_cache

from studip_fuse.avfs.real_path import RealPath

cache = alru_cache(cache_exceptions=False)


def peek_cache(cached_func, *fn_args, **fn_kwargs):
    from functools import _make_key
    if getattr(cached_func, "__self__", None):
        fn_args = (cached_func.__self__,) + fn_args
    key = _make_key(fn_args, fn_kwargs, False)
    return cached_func._cache.get(key)


class CachingRealPath(RealPath.with_middleware(cache, cache), RealPath):
    async def getxattr(self):
        xattrs = await super(CachingRealPath, self).getxattr()
        if self.is_folder:
            fut = peek_cache(self.list_contents)
            if fut is not None:
                if fut.done():
                    try:
                        exc = fut.exception()
                    except CancelledError as e:
                        exc = e
                    if exc:
                        xattrs["contents-status"] = "failed"
                        xattrs["contents-exception"] = exc
                    else:
                        xattrs["contents-status"] = "available"
                        xattrs["contents-exception"] = ""
                else:
                    xattrs["contents-status"] = "pending"
                    xattrs["contents-exception"] = "InvalidStateError: operation is not complete yet"
            else:
                xattrs["contents-status"] = "unknown"
                xattrs["contents-exception"] = "InvalidStateError: operation was not started yet"
        if isinstance(xattrs.get("contents-exception", None), BaseException):
            exc = xattrs["contents-exception"]
            xattrs["contents-exception"] = "%s: %s" % (type(exc).__name__, exc)
        return xattrs

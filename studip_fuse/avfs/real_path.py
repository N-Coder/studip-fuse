import asyncio
import logging
import os
from collections import defaultdict
from typing import Dict, List, Optional, Set

import attr
from cached_property import cached_property
from more_itertools import one, unique_everseen
from pyrsistent import freeze

from studip_fuse.avfs.path_util import normalize_path, path_head, path_name, path_tail
from studip_fuse.avfs.virtual_path import VirtualPath

log = logging.getLogger(__name__)


@attr.s(frozen=True, str=False)
class RealPath(object):
    parent = attr.ib()  # type: 'RealPath'
    generating_vps = attr.ib(converter=freeze)  # type: Set[VirtualPath]

    @generating_vps.validator
    def validate(self, *_):
        assert self.path is not None
        assert self.is_folder is not None
        assert len(self.generating_vps) == 1 or self.is_folder

    @cached_property
    def path(self) -> str:
        return one(unique_everseen(vp.partial_path for vp in self.generating_vps))

    @cached_property
    def is_folder(self) -> bool:
        return one(unique_everseen(vp.is_folder for vp in self.generating_vps))

    @cached_property
    def is_root(self) -> bool:
        return not self.parent

    def __str__(self):
        return self.path + ("[root]" if self.is_root else "") + \
               (" *%s" % len(self.generating_vps) if len(self.generating_vps) > 1 else "")

    async def access(self, mode):
        for vp in self.generating_vps:
            await vp.access(mode)

    async def getattr(self):
        st = {}
        # if multiple Folders that generate the same VirtualPath have different attrs,
        # the actual attrs of the final RealPath may be non-deterministic
        for vp in self.generating_vps:
            st.update(await vp.getattr())
        return {key: val for key, val in st.items() if key in
                ('st_atime', 'st_ctime', 'st_gid', 'st_mode', 'st_mtime', 'st_nlink', 'st_size', 'st_uid')}

    async def getxattr(self):
        xattrs = {}
        for vp in self.generating_vps:
            xattrs.update(await vp.getxattr())
        return xattrs

    async def open_file(self, flags):
        return await one(self.generating_vps).open_file(flags)

    @classmethod
    def with_middleware(cls, resolve_annotation, list_contents_annotation, name="GenericMiddlewareRealPath"):
        return type(name, (cls,), {
            "resolve": resolve_annotation(cls.resolve),
            "list_contents": list_contents_annotation(cls.list_contents),
        })

    async def resolve(self, rel_path) -> Optional['RealPath']:
        rel_path = normalize_path(rel_path)
        if rel_path == "":
            return self

        if os.name == 'nt':
            # on Windows, all file names will be converted to upper case
            eq = lambda x, y: x.upper() == y.upper()
        else:
            eq = lambda x, y: x == y

        resolved_real_file = None
        for content_file in await self.list_contents():
            if eq(rel_path, content_file.path):  # Exact Match
                resolved_real_file = content_file
                break
            elif eq(path_head(rel_path), path_name(content_file.path)):  # Found Parent
                resolved_real_file = await content_file.resolve(path_tail(rel_path))
                break
            else:  # Other File
                continue
        return resolved_real_file

    async def list_contents(self) -> List['RealPath']:
        # merge duplicate sub-entries by putting them in the same Set
        # (required e.g. for folder with lecture name and subfolder with course type)
        contents = defaultdict(set)  # type: Dict[str, Set[VirtualPath]]

        # initialize the set with the root paths
        for root_vp in self.generating_vps:
            contents[root_vp.partial_path].add(root_vp)
        assert len(contents) == 1, "generating VirtualPaths %s of RealPath %s don't have the same path: %s" % \
                                   (self.generating_vps, self, contents)

        async def flatten(no_progress_vp):
            async for sub_vp in no_progress_vp.list_contents():
                assert sub_vp != no_progress_vp, "no_progress_vp %s returned self amongst its contents!" % no_progress_vp
                contents[sub_vp.partial_path].add(sub_vp)

        # skip paths that make no progress
        # (required e.g. for the VirtualPath for "Allgemeiner Dateiordner")
        while contents.get(self.path, None):
            log.debug("Flattening %s paths that are still on the initial level of '%s'...",
                      len(contents[self.path]), self)
            # call `list_contents` for all `no_progress_vp`s in parallel and await completion by gathering the Tasks
            await asyncio.gather(*(
                flatten(no_progress_vp) for no_progress_vp in contents.pop(self.path)
            ))

        return [self.__class__(self, vps) for vps in contents.values()]

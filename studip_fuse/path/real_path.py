import logging
import os
from asyncio import gather
from typing import Dict, List, Optional, Set

import attr
from cached_property import cached_property
from more_itertools import one, unique_everseen

from studip_fuse.cache import cached_task
from studip_fuse.path.path_util import normalize_path, path_head, path_name, path_tail
from studip_fuse.path.virtual_path import VirtualPath

iter_log = logging.getLogger("studip_fuse.real_path.resolve")


@attr.s(frozen=True, str=False, repr=False, hash=False)
class RealPath(object):
    parent = attr.ib()  # type: 'RealPath'
    generating_vps = attr.ib()  # type: Set[VirtualPath]

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

    @cached_task()
    async def resolve(self, rel_path) -> Optional['RealPath']:
        rel_path = normalize_path(rel_path)
        iter_log.debug("Resolving path '%s' relative to '%s'", rel_path, self)

        if rel_path == "":
            iter_log.debug("Resolving empty relative path to self")
            return self

        if os.name == 'nt':
            # on Windows, all file names will be converted to upper case
            eq = lambda x, y: x.upper() == y.upper()
        else:
            eq = lambda x, y: x == y

        resolved_real_file = content_file = None
        for content_file in await self.list_contents():
            if eq(rel_path, content_file.path):  # Exact Match
                resolved_real_file = content_file
                break
            elif eq(path_head(rel_path), path_name(content_file.path)):  # Found Parent
                resolved_real_file = await content_file.resolve(path_tail(rel_path))
                break
            else:  # Other File
                continue

        if resolved_real_file:
            iter_log.debug("Resolved path '%s // %s' to '%s'", self, rel_path, resolved_real_file)
            return resolved_real_file
        else:
            iter_log.debug("No such file or directory '%s // %s'!", self, rel_path)
            return None

    @cached_task()
    async def list_contents(self) -> List['RealPath']:
        # merge duplicate sub-entries by putting them in the same Set
        # (required e.g. for folder with lecture name and subfolder with course type)
        contents = dict()  # type: Dict[str, Set[VirtualPath]]

        # initialize the set with the root paths
        for root_vp in self.generating_vps:
            contents.setdefault(root_vp.partial_path, set()).add(root_vp)
        assert len(contents) == 1  # root paths must have the same effective path
        iter_log.debug("Got %s VirtualPaths generating path '%s', listing contents...",
                       len(contents[self.path]), self)

        async def __update_contents_map(no_progress_vp):
            for sub_vp in await no_progress_vp.list_contents():
                assert sub_vp != no_progress_vp, "no_progress_vp %s returned self amongst its contents!" % no_progress_vp
                contents.setdefault(sub_vp.partial_path, set()).add(sub_vp)

        # skip paths that make no progress
        # (required e.g. for the VirtualPath for "Allgemeiner Dateiordner")
        while contents.get(self.path, None):
            iter_log.debug("Flattening %s paths that are still on the initial level of '%s'...",
                           len(contents[self.path]), self)
            # call `list_contents` for all `no_progress_vp`s in parallel and await completion by gathering the Tasks
            await gather(*(
                __update_contents_map(no_progress_vp) for no_progress_vp in contents.pop(self.path)
            ))

        return [RealPath(self, vps) for vps in contents.values()]

    def access(self, mode):
        for vp in self.generating_vps:
            vp.access(mode)

    def getattr(self):
        st = {}
        # if multiple Folders that generate the same VirtualPath have different attrs,
        # the actual attrs of the final RealPath may be non-deterministic
        for vp in self.generating_vps:
            st.update(vp.getattr())
        return {key: val for key, val in st.items() if key in
                ('st_atime', 'st_ctime', 'st_gid', 'st_mode', 'st_mtime', 'st_nlink', 'st_size', 'st_uid')}

    def open_file(self, flags):
        return one(self.generating_vps).open_file(flags)

    def __str__(self):
        return self.path + ("[root]" if self.is_root else "") + \
               (" *%s" % len(self.generating_vps) if len(self.generating_vps) > 1 else "")

    def __repr__(self):
        return "RealPath(%s)" % str(self)

    def __hash__(self):
        return hash(self.path)

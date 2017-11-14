import functools
import logging
from asyncio import gather
from io import BytesIO
from typing import Callable, Dict, List, Optional, Set

import attr
from cached_property import cached_property
from more_itertools import one, unique_everseen

from studip_fuse.async_cache import schedule_task
from studip_fuse.path_util import normalize_path, path_head, path_name, path_tail
from studip_fuse.virtual_path import VirtualPath

log = logging.getLogger("studip_fuse.real_path")
iter_log = log.getChild("resolve")
iter_log.setLevel(logging.INFO)


@attr.s(frozen=True, str=False, repr=False, hash=False)
class RealPath(object):
    parent: 'RealPath' = attr.ib()
    generating_vps: Set[VirtualPath] = attr.ib()

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

    async def iterate_hierarchically(self, visitor: Callable[['RealPath', int], bool], level: int = 1):
        contents = await self.list_contents()
        iter_log.debug("Found %s unique children of '%s', recursing...", len(contents), self)
        futures = []
        for file in contents:
            go_deeper = visitor(file, level)  # TODO cancellation / StopIteration?
            if go_deeper and self.is_folder:
                futures.append(
                    file.iterate_hierarchically(visitor, level + 1)
                )
        if futures:
            await gather(*futures)

    @functools.lru_cache()
    @schedule_task()
    async def resolve(self, rel_path) -> Optional['RealPath']:
        rel_path = normalize_path(rel_path)
        iter_log.debug("Resolving path '%s' relative to '%s'", rel_path, self)

        if rel_path == "":
            iter_log.debug("Resolving empty relative path to self")
            return self

        resolved_real_file = content_file = None
        for content_file in await self.list_contents():
            if rel_path == content_file.path:  # Exact Match
                resolved_real_file = content_file
                break
            elif path_head(rel_path) == path_name(content_file.path):  # Found Parent
                resolved_real_file = await content_file.resolve(path_tail(rel_path))
                break
            else:  # Other File
                continue

        if resolved_real_file:
            iter_log.debug("Resolved path '%s // %s' to '%s'", self, rel_path, resolved_real_file)
            return resolved_real_file
        else:
            iter_log.debug("No such file or directory '%s // %s'!", self, rel_path, rel_path, content_file)
            return None

    @functools.lru_cache()
    @schedule_task()
    async def list_contents(self) -> List['RealPath']:
        # merge duplicate sub-entries by putting them in the same Set
        # (required e.g. for folder with lecture name and subfolder with course type)
        contents: Dict[str, Set[VirtualPath]] = dict()

        # initialize the set with the root paths
        for root_vp in self.generating_vps:
            contents.setdefault(root_vp.partial_path, set()).add(root_vp)
        assert len(contents) == 1  # root paths must have the same effective path
        iter_log.debug("Got %s VirtualPaths generating path '%s', listing contents...",
                       len(contents[self.path]), self)

        async def __update_contents_map(no_progress_vp):
            for sub_vp in await no_progress_vp.list_contents():
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
        return 0  # TODO reduce

    def getattr(self):
        st = {}
        for vp in self.generating_vps:  # TODO sort
            st.update(vp.getattr())
        return {key: val for key, val in st.items() if key in
                ('st_atime', 'st_ctime', 'st_gid', 'st_mode', 'st_mtime', 'st_nlink', 'st_size', 'st_uid')}

    def open_file(self, flags) -> BytesIO:
        return one(self.generating_vps).open_file(flags)

    def __str__(self):
        return self.path + ("[root]" if self.is_root else "") + \
               (" *%s" % len(self.generating_vps) if len(self.generating_vps) > 1 else "")

    def __repr__(self):
        return "RealPath(%s)" % str(self)

    def __hash__(self):
        return hash(self.path)

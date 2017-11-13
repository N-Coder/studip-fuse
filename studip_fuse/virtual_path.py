import functools
import itertools
import logging
from datetime import datetime
from io import BytesIO
from os import path
from stat import S_IFDIR, S_IFREG
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Type

import attr
from cached_property import cached_property
from more_itertools import one, unique_everseen

from studip_api.model import Course, File, Folder, Semester
from studip_fuse.async_fetch import CachedStudIPSession
from studip_fuse.path_util import Charset, EscapeMode, escape_file_name, normalize_path, path_head, path_name, path_tail

log = logging.getLogger("studip_fuse.virtual_path")
iter_log = log.getChild("resolve")
iter_log.setLevel(logging.INFO)


@attr.s(frozen=True, str=False, repr=False, hash=False)
class VirtualPath(object):
    session: CachedStudIPSession = attr.ib()
    path_segments: List[str] = attr.ib()  # {0,n}
    known_data: Dict[Type, Any] = attr.ib()
    parent: Optional['VirtualPath'] = attr.ib()
    next_path_segments: List[str] = attr.ib()

    # __init__  ########################################################################################################

    @known_data.validator
    def validate(self, *_):
        if not self.is_folder:
            assert self._file
        if self._course:
            assert not self._semester or self._course.semester == self._semester
        if self._file:
            assert not self._semester or self._file.course.semester == self._semester
            assert not self._course or self._file.course == self._course
        assert Folder not in self.known_data

    # public properties  ###############################################################################################

    @cached_property
    def partial_path(self):
        path_segments = self.path_segments
        if self._loop_over_path and self._file:
            # preview the file path we're generating in the loop
            path_segments = path_segments + [path_head(self.next_path_segments)]
        partial = "/".join(path_segments).format(**self._known_tokens)
        partial = normalize_path(partial)
        return partial

    @cached_property
    def is_folder(self) -> bool:
        return bool(self.next_path_segments)

    @cached_property
    def is_root(self) -> bool:
        return not self.parent

    # FS-API  ##########################################################################################################

    @functools.lru_cache()
    def list_contents(self) -> List['VirtualPath']:
        assert self.is_folder

        if File in self._content_options:
            return self._list_contents_file_options()

        elif Course in self._content_options:
            if self._course:  # everything is already known, no options on this level
                return [self._sub_path()]
            return [self._sub_path(new_known_data={Course: c})
                    for c in itertools.chain(*self.session.courses.result())
                    if (not self._semester or c.semester == self._semester)]

        elif Semester in self._content_options:
            if self._semester:  # everything is already known, no options on this level
                return [self._sub_path()]
            return [self._sub_path(new_known_data={Semester: s})
                    for s in self.session.semesters.result()]

        else:
            assert "{" not in path_head(self.next_path_segments)  # static name
            return [self._sub_path()]

    def _list_contents_file_options(self):
        assert self.is_folder

        if self._file:
            if self._loop_over_path and self._file.is_folder():  # loop over contents of one folder #1
                if self._file.contents is None:
                    log.warning("Contents of %s were not retrieved, assuming empty", self._file)
                    return []
                files = self._file.contents  # TODO contents should be a future, too
            else:  # everything is already known, no options on this level
                return [self._sub_path()]
        else:  # all folders still possible
            files = (f for f in self.session.files.result() if
                     isinstance(f, File) and
                     (not self._file or f.parent == self._file) and  # TODO self._file is always False
                     (not self._course or f.course == self._course) and
                     (not self._semester or f.course.semester == self._semester))

        return [self._sub_path(new_known_data={File: f}, increment_path_segments=not self._loop_over_path)
                for f in files]

    def access(self, mode):
        pass  # TODO Implement
        # if not os.access(sub_vps[0].cache_path, mode):
        #     raise FuseOSError(errno.EACCES)

    def getattr(self):
        d = dict(st_mode=(S_IFDIR if self.is_folder else S_IFREG) | 0o755, st_nlink=2)
        if self.mod_times[0]:
            d["st_ctime"] = self.mod_times[0].timestamp()
        if self.mod_times[1]:
            d["st_mtime"] = self.mod_times[1].timestamp()
        return d

    def open_file(self, flags) -> BytesIO:  # blocking,
        assert not self.is_folder
        return None
        # TODO implement, download missing files to cache
        # return os.open(sub_vps[0].cache_path, flags)

    # private properties ###############################################################################################

    def __escape_file(self, str):
        return escape_file_name(str, Charset.Ascii, EscapeMode.Similar)

    def __escape_path(self, folders):
        return path.join(*map(self.__escape_file, folders)) if folders else ""

    @cached_property
    def mod_times(self) -> Tuple[Optional[datetime], Optional[datetime]]:
        if self._file:
            return self._file.created, self._file.changed
        if self._course:
            return (self._course.semester.start_date,) * 2
        if self._semester:
            return (self._semester.start_date,) * 2
        return None, None

    @cached_property
    def _known_tokens(self):
        tokens = {
            "created": self.mod_times[0],
            "changed": self.mod_times[1],
        }
        if self._semester:
            tokens.update({
                "semester": self.__escape_file(self._semester.name),
                "semester-lexical": self.__escape_file(self._semester.lexical),
                "semester-lexical-short": self.__escape_file(self._semester.lexical_short),
            })

        if self._course:
            tokens.update({
                "semester": self.__escape_file(self._course.semester.name),
                "semester-lexical": self.__escape_file(self._course.semester.lexical),
                "semester-lexical-short": self.__escape_file(self._course.semester.lexical_short),

                "course-id": self._course.id,
                "course-abbrev": self.__escape_file(self._course.abbrev),
                "course": self.__escape_file(self._course.name),
                "type": self.__escape_file(self._course.type),
                "type-abbrev": self.__escape_file(self._course.type_abbrev),
            })

        if self._file:
            path = short_path = self._file.path.split("/")[2:-1]  # ''/'1234VL-Name'/[path]/'file_name'
            if short_path[0:1] == ["Allgemeiner Dateiordner"]:
                short_path = path_tail(short_path)

            tokens.update({
                "semester": self.__escape_file(self._file.course.semester.name),
                "semester-lexical": self.__escape_file(self._file.course.semester.lexical),
                "semester-lexical-short": self.__escape_file(self._file.course.semester.lexical_short),

                "course-id": self._file.course.id,
                "course-abbrev": self.__escape_file(self._file.course.abbrev),
                "course": self.__escape_file(self._file.course.name),
                "type": self.__escape_file(self._file.course.type),
                "type-abbrev": self.__escape_file(self._file.course.type_abbrev),

                "path": self.__escape_path(path),
                "short-path": self.__escape_path(short_path),

                "id": self._file.id,
                "name": self.__escape_file(self._file.name),
                "description": self.__escape_file(self._file.description or ""),
                "author": self.__escape_file(self._file.author or ""),
                # "ext": extension,
                # "descr-no-ext": self.__escape_file(descr_no_ext),
            })
        return tokens

    @cached_property
    def _file(self) -> File:
        return self.known_data.get(File, None)

    @cached_property
    def _course(self) -> Course:
        return self.known_data.get(Course, None)

    @cached_property
    def _semester(self) -> Semester:
        return self.known_data.get(Semester, None)

    @cached_property
    def _loop_over_path(self):
        return self.is_folder and any(t in path_head(self.next_path_segments) for t in ["{path}", "{short-path}"])

    @cached_property
    def _content_options(self) -> Set[Type]:
        if self.is_folder:
            return get_format_segment_requires(path_head(self.next_path_segments))
        else:
            return set()

    def _sub_path(self, new_known_data=None, increment_path_segments=True, **kwargs):
        assert self.is_folder
        args = dict(session=self.session, parent=self, known_data=self.known_data)
        if increment_path_segments:
            args.update(path_segments=self.path_segments + [path_head(self.next_path_segments)],
                        next_path_segments=path_tail(self.next_path_segments))
        else:
            args.update(path_segments=self.path_segments,
                        next_path_segments=self.next_path_segments)
        if new_known_data:
            args["known_data"] = dict(args["known_data"])
            args["known_data"].update(new_known_data)
        args.update(kwargs)
        return VirtualPath(**args)

    # utils  ###########################################################################################################

    def __hash__(self):
        return hash(self.partial_path)

    def __str__(self):
        path_segments = [seg.format(**self._known_tokens) for seg in self.path_segments]

        if self._loop_over_path and self._file:
            # preview the file path we're generating in the loop
            preview_file_path = path_head(self.next_path_segments).format(**self._known_tokens)
            if preview_file_path:
                path_segments.append("(" + preview_file_path + ")")

        path_segments += self.next_path_segments

        options = "[%s]->[%s]" % (
            ",".join(c.__name__ for c in self.known_data.keys()),
            ",".join(c.__name__ for c in self._content_options))
        return "[%s](%s)" % (
            "/".join(filter(bool, path_segments)),
            ",".join(filter(bool, [
                "root" if self.is_root else None,
                "folder" if self.is_folder else "file",
                "loop_path" if self._loop_over_path else None,
                options
            ]))
        )


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

    def iterate_hierarchically(self, visitor: Callable[['RealPath', int], bool], level: int = 1):
        contents = self.list_contents()
        iter_log.debug("Found %s unique children of '%s', recursing...", len(contents), self)
        for file in contents:
            go_deeper = visitor(file, level)  # TODO cancellation / StopIteration?
            if go_deeper and self.is_folder:
                file.iterate_hierarchically(visitor, level + 1)

    @functools.lru_cache()
    def resolve(self, rel_path):  # TODO cache async coroutine
        rel_path = normalize_path(rel_path)
        iter_log.debug("Resolving path '%s' relative to '%s'", rel_path, self)

        if rel_path == "":
            iter_log.debug("Resolving empty relative path to self")
            return self

        resolved_real_file = content_file = None
        for content_file in self.list_contents():
            if rel_path == content_file.path:  # Exact Match
                resolved_real_file = content_file
                break
            elif path_head(rel_path) == path_name(content_file.path):  # Found Parent
                resolved_real_file = content_file.resolve(path_tail(rel_path))
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
    def list_contents(self) -> List['RealPath']:  # TODO cache async coroutine
        # merge duplicate sub-entries by putting them in the same Set
        # (required e.g. for folder with lecture name and subfolder with course type)
        contents: Dict[str, Set[VirtualPath]] = dict()

        # initialize the set with the root paths
        for root_vp in self.generating_vps:
            contents.setdefault(root_vp.partial_path, set()).add(root_vp)
        assert len(contents) == 1  # root paths must have the same effective path
        iter_log.debug("Got %s VirtualPaths generating path '%s', listing contents...",
                       len(contents[self.path]), self)

        # skip paths that make no progress
        # (required e.g. for the VirtualPath for "Allgemeiner Dateiordner")
        while contents.get(self.path, None):
            iter_log.debug("Flattening %s paths that are still on the initial level of '%s'...",
                           len(contents[self.path]), self)
            for no_progress_vp in contents.pop(self.path):
                assert no_progress_vp.is_folder
                for sub_vp in no_progress_vp.list_contents():
                    contents.setdefault(sub_vp.partial_path, set()).add(sub_vp)

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
        return self.path + (" *%s" % len(self.generating_vps) if len(self.generating_vps) > 1 else "")

    def __hash__(self):
        return hash(self.path)


def get_format_segment_requires(format_segment) -> Set[Type]:
    # FIXME breaks when value formatting options are used
    requirements = set()
    if any(t in format_segment for t in ["{semester}", "{semester-lexical}", "{semester-lexical-short}"]):
        requirements.add(Semester)
    if any(t in format_segment for t in ["{course}", "{course-abbrev}", "{course-id}", "{type}", "{type-abbrev}"]):
        requirements.add(Course)
    if any(t in format_segment for t in ["{path}", "{short-path}", "{id}", "{name}", "{description}", "{author}"]):
        requirements.add(File)
    if "{time}" in format_segment and not requirements:  # any info can provide a time
        requirements.add(Semester)
    return requirements

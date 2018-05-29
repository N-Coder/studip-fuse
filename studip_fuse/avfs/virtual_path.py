import logging
from abc import abstractmethod
from itertools import chain
from string import Formatter
from typing import Any, Dict, List, NewType, Optional, Set, Tuple, Type, Union

import attr
from cached_property import cached_property
from frozendict import frozendict
from tabulate import tabulate

from studip_fuse.avfs.path_util import normalize_path, path_head, path_tail

log = logging.getLogger(__name__)
FORMATTER = Formatter()

__all__ = ["FormatToken", "DataField", "get_format_str_fields", "VirtualPath"]

FormatToken = NewType("FormatToken", str)
DataField = NewType("DataField", str)


def get_format_str_fields(format_segment) -> Set[Type]:
    for (literal_text, field_name, format_spec, conversion) in FORMATTER.parse(format_segment):
        yield field_name


@attr.s(frozen=True, str=False)
class VirtualPath(object):
    parent = attr.ib()  # type: Optional['VirtualPath']
    path_segments = attr.ib(convert=tuple)  # type: Tuple[Union[str, FormatToken]]
    known_data = attr.ib(convert=frozendict)  # type: frozendict[DataField, Any]
    next_path_segments = attr.ib(convert=tuple)  # type: Tuple[Union[str, FormatToken]]

    # __init__  ########################################################################################################

    def __attrs_post_init__(self):
        self.validate()

    def validate(self):
        if self.parent:
            assert self.partial_path.startswith(self.parent.partial_path), \
                "Path of child file %s doesn't start with the path of its parent %s. " \
                "Does your path format specification make sense?" % (self, self.parent)
            assert set(self.known_tokens.items()).issuperset(self.parent.known_tokens.items()), \
                "Known data of child file %s doesn't include all data of its parent %s. " \
                "Does your path format specification make sense? " \
                "Offending keys are:\n%s" % (self, self.parent, tabulate(
                    ((key, self.parent.known_tokens.get(key, "unset"), self.known_tokens.get(key, "unset"))
                     for key in set(chain(self.known_tokens.keys(), self.parent.known_tokens.keys()))),
                    headers=["key", "parent value", "child value"], missingval="None"
                ))

    # public properties  ###############################################################################################

    @cached_property
    def partial_path(self) -> str:
        path_segments = self.path_segments
        if self.segment_needs_expand_loop:
            # preview the file path we're generating in the loop
            path_segments = path_segments + (path_head(self.next_path_segments),)
        partial = "/".join(path_segments).format(**self.known_tokens)
        partial = normalize_path(partial)
        return partial

    @cached_property
    def is_folder(self) -> bool:
        return bool(self.next_path_segments)

    @cached_property
    def is_root(self) -> bool:
        return not self.parent

    # abstract properties  #############################################################################################

    @property
    @abstractmethod
    def content_options(self) -> Set[DataField]:
        """
        Returns a hint on which new known DataField should be set by the child generated in list_contents.
        __mk_sub_path assumes that the keys of new_known_data are a subset of the returned set.
        """
        pass

    @property
    @abstractmethod
    def segment_needs_expand_loop(self) -> bool:
        """
        Whether the currently generated path segment contains a token that is itself a path,
        that might be appended in the next step.
        """
        pass

    @property
    @abstractmethod
    def known_tokens(self) -> Dict[FormatToken, Any]:
        """
        The already known format tokens for generating the partial path from the format string template.
        This converts the Dict[DataField, Any] known_data to a Dict[FormatToken, Any],
        mapping known DataFields to derived FormatTokens.
        """
        pass

    # FS-API  ##########################################################################################################

    async def access(self, mode):
        pass

    @abstractmethod
    async def getattr(self) -> Dict[str, int]:
        pass

    @abstractmethod
    async def open_file(self, flags) -> Any:
        pass

    @abstractmethod
    async def list_contents(self) -> List['VirtualPath']:
        pass

    # utils  ###########################################################################################################

    def _mk_sub_path(self, new_known_data: Dict[DataField, Any] = None, increment_path_segments=True, **kwargs):
        assert self.is_folder, "__mk_sub_path called on non-folder %s" % self
        args = attr.asdict(self, recurse=False)
        args["parent"] = self
        args["known_data"] = dict(self.known_data)
        if increment_path_segments:
            args.update(path_segments=self.path_segments + (path_head(self.next_path_segments),),
                        next_path_segments=path_tail(self.next_path_segments))
        else:
            args.update(path_segments=self.path_segments,
                        next_path_segments=self.next_path_segments)
        if new_known_data:
            args["known_data"].update(new_known_data)
        args.update(kwargs)
        return self.__class__(**args)

    def __str__(self):
        path_segments = [seg.format(**self.known_tokens) for seg in self.path_segments]

        if self.segment_needs_expand_loop and self._file:
            # preview the file path we're generating in the loop
            preview_file_path = path_head(self.next_path_segments).format(**self.known_tokens)
            if preview_file_path:
                path_segments.append("(" + preview_file_path + ")")

        path_segments += self.next_path_segments

        options = "[%s]->[%s]" % (
            ",".join(str(v) for v in self.known_data.keys()),
            ",".join(str(v) for v in self.content_options))
        return "[%s](%s)" % (
            "/".join(filter(bool, path_segments)),
            ",".join(filter(bool, [
                "root" if self.is_root else None,
                "folder" if self.is_folder else "file",
                "loop_path" if self.segment_needs_expand_loop else None,
                options
            ]))
        )

import logging
from abc import abstractmethod
from string import Formatter
from typing import Any, Dict, List, NewType, Optional, Set, Tuple, Type, Union

import attr
from cached_property import cached_property
from pyrsistent import freeze
from tabulate import tabulate

from studip_fuse.avfs.path_util import commonpath, join_path, normalize_path, path_head, path_tail

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
    path_segments = attr.ib(convert=freeze)  # type: Tuple[Union[str, FormatToken]]
    known_data = attr.ib(convert=freeze)  # type: Dict[DataField, Any]
    next_path_segments = attr.ib(convert=freeze)  # type: Tuple[Union[str, FormatToken]]

    # __init__  ########################################################################################################

    def __attrs_post_init__(self):
        self.validate()

    def validate(self):
        if self.parent:
            assert commonpath([self.partial_path, self.parent.partial_path]) == self.parent.partial_path, \
                "Path of child file %s doesn't start with the path of its parent %s. " \
                "Does your path format specification make sense?" % (self, self.parent)
            changed_tokens = set(self.known_tokens.items()).difference(self.parent.known_tokens.items())
            if not changed_tokens:
                return
            offending_tokens = []
            for key, child_value in changed_tokens:
                if key not in self.parent.known_tokens:
                    continue  # key added in child
                parent_value = self.parent.known_tokens[key]
                assert parent_value != child_value
                if not self.segment_needs_expand_loop:
                    offending_tokens.append((key, parent_value, child_value, "(child values can only grow if segment_needs_expand_loop is True)"))
                elif not child_value.startswith(parent_value):
                    offending_tokens.append((key, parent_value, child_value, "(child value is not an expansion of the parent value)"))
                elif key in self.path_segments:
                    offending_tokens.append((key, parent_value, child_value, "(already set path_segments may not change)"))
                else:
                    continue  # expansion of non-fixed child value is allowed in expand loop
            if offending_tokens:
                raise AssertionError(
                    "Known data of child file %s doesn't include all data of its parent %s. "
                    "Does your path format specification make sense? "
                    "Offending tokens are:\n%s" % (self, self.parent, tabulate(
                        offending_tokens,
                        headers=["key", "parent value", "child value", "error"], missingval="None"
                    ))
                )

    # public properties  ###############################################################################################

    @cached_property
    def partial_path(self) -> str:
        partial = join_path(*self.path_segments)
        try:
            partial = partial.format_map(self.known_tokens)
        except KeyError:
            missing_fields = set(get_format_str_fields(partial)).difference(self.known_tokens.keys())
            assert not missing_fields, "Format specification '%s' is missing fields %s in known tokens of virtual path %s" % \
                                       (partial, missing_fields, self)
            raise
        partial = normalize_path(partial)
        return partial

    @cached_property
    def is_folder(self) -> bool:
        return self.next_path_segments or self.segment_needs_expand_loop

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

    @classmethod
    def with_middleware(cls, list_contents_annotation, open_file_annotation, name="GenericMiddlewareStudIPPath"):
        return type(name, (cls,), {
            "list_contents": list_contents_annotation(cls.list_contents),
            "open_file": open_file_annotation(cls.open_file),
        })

    async def access(self, mode):
        pass

    @abstractmethod
    async def getattr(self) -> Dict[str, int]:
        pass

    @abstractmethod
    async def getxattr(self) -> Dict[str, str]:
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
        details = [
            "root" if self.is_root else None,
            "folder" if self.is_folder else "file",
            "loop_path" if self.segment_needs_expand_loop else None
        ]

        try:
            path_segments = [seg.format_map(self.known_tokens) for seg in self.path_segments]
        except KeyError:
            # something is weird, we don't have all known_tokens to fulfill the current path format specification
            # still try to complete str generation with as much debug information as possible
            # and let `partial_path` (called in `validate`) raise the corresponding exception
            details.append("known tokens incomplete: %s" % set(self.known_tokens.keys()))

            path_segments = []
            for seg in self.path_segments:
                missing_fields = set(get_format_str_fields(seg)).difference(self.known_tokens.keys())
                if missing_fields:
                    path_segments.append("%s[!missing %s!]" % (seg, missing_fields))
                else:
                    path_segments.append(seg)

        details.append("[%s]->[%s]" % (
            ",".join(str(v) for v in self.known_data.keys()),
            ",".join(str(v) for v in self.content_options)))
        return "[%s](%s)" % (
            "/".join(filter(bool, path_segments)),
            ",".join(filter(bool, details))
        )

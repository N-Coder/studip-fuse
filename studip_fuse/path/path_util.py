import re
from enum import IntEnum
from os.path import normpath
from typing import Set, Type

from studip_api.model import Course, File, Semester

__all__ = ["EscapeMode", "Charset", "escape_file_name", "normalize_path", "path_head", "path_tail", "path_parent",
           "path_name", "get_format_segment_requires"]

PUNCTUATION_WHITESPACE_RE = re.compile(r"[ _/.,;:\-_#'+*~!^\"$%&/()[\]}{\\?<>|]+")
NON_ASCII_RE = re.compile(r"[^\x00-\x7f]+")
NON_IDENTIFIER_RE = re.compile(r"[^A-Za-z0-9_]+")
FS_SPECIAL_CHARS_RE = re.compile(r"[/:]+")
EscapeMode = IntEnum("EscapeMode", "Similar Typeable CamelCase SnakeCase")
Charset = IntEnum("Charset", "Unicode Ascii Identifier")


def escape_file_name(str, charset, mode):
    if charset in [Charset.Ascii, Charset.Identifier]:
        str = str.replace("ß", "ss").replace("ä", "ae").replace("Ä", "Ae") \
            .replace("ö", "oe").replace("Ö", "Oe").replace("ü", "ue") \
            .replace("Ü", "Ue")
        str = (NON_ASCII_RE if charset == Charset.Ascii else NON_IDENTIFIER_RE).sub("", str)
    if mode in [EscapeMode.SnakeCase, EscapeMode.CamelCase] or charset == Charset.Identifier:
        parts = PUNCTUATION_WHITESPACE_RE.split(str)
        if mode == EscapeMode.SnakeCase:
            return "_".join(parts).lower()
        elif mode == EscapeMode.CamelCase:
            return "".join(w[0].upper() + w[1:] for w in parts if len(w) > 0)
        else:
            return "_".join(parts)
    elif mode == EscapeMode.Typeable or charset in [Charset.Ascii, Charset.Identifier]:
        return FS_SPECIAL_CHARS_RE.sub("-" if charset == Charset.Ascii else "_", str)
    else:  # mode == "unicode" or incorrectly set
        # Replace regular '/' by similar looking 'DIVISION SLASH' (U+2215) and ':' by
        # 'RATIO' to create a valid directory name
        return str.replace("/", "\u2215").replace(":", "\u2236")


def normalize_path(p):
    p = normpath(p)
    while p.startswith("/"):
        p = p[1:]
    while p.endswith("/"):
        p = p[:-1]
    if p == ".":
        p = ""
    return p


def path_head(p):
    if isinstance(p, str):
        if "/" in p:
            return p[:p.index("/")]
        else:
            return p
    else:
        return p[0]


def path_tail(p):
    if isinstance(p, str):
        if "/" in p:
            return p[p.index("/") + 1:]
        else:
            return ""
    else:
        return p[1:]


def path_parent(p):
    if isinstance(p, str):
        return p[:p.rfind("/")]
    else:
        return p[:-1]


def path_name(p):
    if isinstance(p, str):
        return p[p.rfind("/") + 1:]
    else:
        return p[-1]


def __test_paths():
    for test_path, (head, tail, parent, name) in {
        "A/B/C/D": ("A", "B/C/D", "A/B/C", "D"),
        "A": ("A", "", "", "A"),
        "": ("",) * 4
    }.items():
        assert path_head(test_path) == head
        assert path_tail(test_path) == tail
        assert path_parent(test_path) == parent
        assert path_name(test_path) == name


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
        # TODO time may differ between file and parent folder, which will break path logic
        requirements.add(Semester)
    return requirements

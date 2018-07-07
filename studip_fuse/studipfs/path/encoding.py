import re
from enum import IntEnum

PUNCTUATION_WHITESPACE_RE = re.compile(r"[ _/.,;:\-#'+*~!^\"$%&()[\]}{\\?<>|]+")
NON_ASCII_RE = re.compile(r"[^\x00-\x7f]+")
NON_IDENTIFIER_RE = re.compile(r"[^A-Za-z0-9_]+")
FS_SPECIAL_CHARS_RE = re.compile(r"[/:]+")
EscapeMode = IntEnum("EscapeMode", "Similar Typeable CamelCase SnakeCase")
Charset = IntEnum("Charset", "Unicode Ascii Identifier")


def escape_file_name(val, charset, mode):
    val = str(val)
    if charset in [Charset.Ascii, Charset.Identifier]:
        val = val.replace("ß", "ss").replace("ä", "ae").replace("Ä", "Ae") \
            .replace("ö", "oe").replace("Ö", "Oe").replace("ü", "ue") \
            .replace("Ü", "Ue")
        val = (NON_ASCII_RE if charset == Charset.Ascii else NON_IDENTIFIER_RE).sub("", val)
    if mode in [EscapeMode.SnakeCase, EscapeMode.CamelCase] or charset == Charset.Identifier:
        parts = PUNCTUATION_WHITESPACE_RE.split(val)
        if mode == EscapeMode.SnakeCase:
            return "_".join(parts).lower()
        elif mode == EscapeMode.CamelCase:
            return "".join(w[0].upper() + w[1:] for w in parts if len(w) > 0)
        else:
            return "_".join(parts)
    elif mode == EscapeMode.Typeable or charset in [Charset.Ascii, Charset.Identifier]:
        return FS_SPECIAL_CHARS_RE.sub("-" if charset == Charset.Ascii else "_", val)
    else:  # mode == "unicode" or incorrectly set
        # Replace regular '/' by similar looking 'DIVISION SLASH' (U+2215) and ':' by
        # 'RATIO' to create a valid directory name
        return val.replace("/", "\u2215").replace(":", "\u2236")

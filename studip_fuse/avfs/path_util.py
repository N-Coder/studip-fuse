import posixpath

__all__ = ["normalize_path", "path_head", "path_tail", "path_parent", "path_name", "join_path", "split_path", "commonpath"]

commonpath = posixpath.commonpath
split_path = posixpath.split


def join_path(*p):
    if not p:
        return ""
    else:
        return posixpath.join(*p)


def normalize_path(p):
    p = posixpath.normpath(p)
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

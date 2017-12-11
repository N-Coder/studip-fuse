import argparse as argparse
import logging
import os

import appdirs
from more_itertools import flatten


def parse_args():
    from studip_fuse import __version__ as prog_version, __author__ as prog_author
    dirs = appdirs.AppDirs("Stud.IP-Fuse", prog_author)

    opts_parser = argparse.ArgumentParser(add_help=False)
    opts_parser.add_argument("-o", help="FUSE-like options", nargs="+", action=StoreNameValuePair(opts_parser))
    debug = opts_parser.add_argument("-d", "--debug", help="enable debug mode", action="store_true")

    studip_opts = opts_parser.add_argument_group("Stud.IP Driver Options")
    studip_opts.add_argument("--pwfile", help="path to password file or '-' to read from stdin",
                             default=os.path.join(dirs.user_config_dir, ".studip-pw"))
    studip_opts.add_argument("--format", help="format specifier for virtual paths",
                             default="{semester-lexical-short}/{course}/{type}/{short-path}/{name}")
    studip_opts.add_argument("--cache", help="path to cache directory", default=dirs.user_cache_dir)
    studip_opts.add_argument("--studip", help="Stud.IP base URL", default="https://studip.uni-passau.de")
    studip_opts.add_argument("--sso", help="SSO base URL", default="https://sso.uni-passau.de")

    fuse_opts = opts_parser.add_argument_group("FUSE Options")
    fuse_opts.add_argument("--foreground", help="run in foreground", action="store_true")
    fuse_opts.add_argument("--nothreads", help="single threads for FUSE", action="store_true")
    fuse_opts.add_argument("--allow_other", help="allow access by all users", action="store_true")
    fuse_opts.add_argument("--allow_root", help="allow access by root", action="store_true")
    fuse_opts.add_argument("--nonempty", help="allow mounts over non-empty file/dir", action="store_true")
    fuse_opts.add_argument("--umask", help="set file permissions (octal)", action="store")
    fuse_opts.add_argument("--uid", help="set file owner", action="store")
    fuse_opts.add_argument("--gid", help="set file group", action="store")
    fuse_opts.add_argument("--default_permissions", help="enable permission checking by kernel",
                           action="store_true")

    http_opts = opts_parser.add_argument_group("HTTP Client Options")
    http_opts.add_argument("--read_timeout", action="store", help="request operations timeout in seconds", default=30,
                           type=float)
    http_opts.add_argument("--conn_timeout", action="store", help="timeout for connection establishing in seconds",
                           default=30, type=float)
    http_opts.add_argument("--keepalive_timeout", action="store",
                           help="timeout for connection reusing after releasing in seconds", default=60, type=float)
    http_opts.add_argument("--limit", action="store", help="total number simultaneous connections", default=10,
                           type=int)
    http_opts.add_argument("--force_close", action="store_true", help="disable HTTP keep-alive")

    parser = argparse.ArgumentParser(description="Stud.IP Fuse", formatter_class=argparse.ArgumentDefaultsHelpFormatter,
                                     parents=[opts_parser])
    parser.add_argument("user", help="Stud.IP username")
    parser.add_argument("mount", help="path to mount point")
    parser.add_argument("-V", "--version", action="version", version="%(prog)s " + prog_version)

    args = parser.parse_args()
    http_args = {a.dest: getattr(args, a.dest, None) for a in http_opts._group_actions}
    fuse_args = {a.dest: getattr(args, a.dest, None) for a in fuse_opts._group_actions + [debug]
                 if getattr(args, a.dest, None) is not None}
    return args, http_args, fuse_args


def StoreNameValuePair(option_parser):
    class anonymous_class(argparse.Action):
        def __call__(self, parser, namespace, values, option_string=None):
            ignored_values = []
            values = flatten(v.split(',') for v in values)
            for value in values:
                if value in ["suid", "nosuid", "dev", "nodev", "ro"]:
                    ignored_values.append(value)
                elif value == "rw":
                    parser.error("Stud.IP FUSE only supports read-only mount")
                else:
                    option_parser.parse_args(["--" + value], namespace)
            if ignored_values:
                logging.debug("Ignoring arguments %s" % ", ".join(ignored_values))

    return anonymous_class

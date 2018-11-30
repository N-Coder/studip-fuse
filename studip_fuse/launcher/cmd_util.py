import argparse as argparse
import logging
import os

import appdirs
from more_itertools import flatten
from yarl import URL


def parse_args():
    from studip_fuse import __version__ as prog_version, __author__ as prog_author
    dirs = appdirs.AppDirs("Stud.IP-Fuse", prog_author)

    opts_parser = argparse.ArgumentParser(add_help=False)
    opts_parser.add_argument("-o", help="FUSE-like options", nargs="+", action=StoreNameValuePair(opts_parser))
    opts_parser.add_argument("-d", "--debug", help="turn on all debugging options", action="store_true")
    opts_parser.add_argument("-v", "--debug-logging", help="turn on debug logging", action="store_true")

    studip_opts = opts_parser.add_argument_group("Stud.IP Driver Options")
    studip_opts.add_argument("--pwfile", help="path to password file or '-' to read from stdin",
                             default=os.path.join(dirs.user_config_dir, ".studip-pw"))
    studip_opts.add_argument("--format", help="format specifier for virtual paths",
                             default="{semester}/{course}/{course-type}/{short-path}/{file-name}")
    studip_opts.add_argument("--cache", help="path to cache directory", default=dirs.user_cache_dir)
    studip_opts.add_argument("--studip", help="Stud.IP API URL", type=URL,
                             default="https://studip.uni-passau.de/studip/api.php/")
    studip_opts.add_argument("--sso", help="Studi.IP SSO URL", type=URL,
                             default="https://studip.uni-passau.de/studip/index.php?again=yes&sso=shib")

    fuse_opts = opts_parser.add_argument_group("FUSE Options")
    fuse_opts.add_argument("-f", "--foreground", help="run in foreground", action="store_true")
    fuse_opts.add_argument("--nothreads", help="single threads for FUSE", action="store_true")
    fuse_opts.add_argument("--allow-other", help="allow access by all users", action="store_true")
    fuse_opts.add_argument("--allow-root", help="allow access by root", action="store_true")
    fuse_opts.add_argument("--nonempty", help="allow mounts over non-empty file/dir", action="store_true")
    fuse_opts.add_argument("--umask", help="set file permissions (octal)", action="store")
    fuse_opts.add_argument("--uid", help="set file owner", action="store")
    fuse_opts.add_argument("--gid", help="set file group", action="store")
    fuse_opts.add_argument("--default-permissions", help="enable permission checking by kernel",
                           action="store_true")
    fuse_opts.add_argument("--debug-fuse", help="enable FUSE debug mode (includes --foreground)", action="store_true")

    http_opts = opts_parser.add_argument_group("HTTP Client Options")

    http_opts.add_argument("--read-timeout", action="store", help="cumulative request operations timeout in seconds",
                           default=30, type=float)  # (connect/queue, request, redirects, responses, data consuming)
    http_opts.add_argument("--conn-timeout", action="store", help="timeout for connection acquiring in seconds",
                           default=30, type=float)  # includes waiting for a pooled connection from an empty pool

    http_opts.add_argument("--keepalive-timeout", action="store",
                           help="timeout for connection reusing after releasing in seconds",
                           default=60, type=float)
    http_opts.add_argument("--limit", action="store", help="total number of simultaneous connections",
                           default=10, type=int)
    http_opts.add_argument("--force-close", action="store_true", help="disable HTTP keep-alive")
    debug_aio = http_opts.add_argument("--debug-aio", help="turn on aiohttp debug logging", action="store_true")

    parser = argparse.ArgumentParser(description="Stud.IP FUSE driver",
                                     formatter_class=argparse.ArgumentDefaultsHelpFormatter,
                                     parents=[opts_parser])
    parser.add_argument("user", help="Stud.IP username")
    parser.add_argument("mount", help="path to mount point")
    parser.add_argument("-V", "--version", action="version", version="%(prog)s " + prog_version)

    args = parser.parse_args()

    if args.debug:
        args.debug_logging = True
        args.debug_fuse = True
        args.debug_aio = True

    http_args = {a.dest: getattr(args, a.dest, None) for a in http_opts._group_actions
                 if a is not debug_aio}
    fuse_args = {a.dest: getattr(args, a.dest, None) for a in fuse_opts._group_actions
                 if getattr(args, a.dest, None) is not None}
    return args, http_args, fuse_args


def StoreNameValuePair(option_parser):
    class anonymous_class(argparse.Action):
        def __call__(self, parser, namespace, values, option_string=None):
            ignored_values = []
            values = flatten(v.split(',') for v in values)
            for value in values:
                if value in ["dev", "nodev", "exec", "noexec", "suid", "nosuid", "ro"]:
                    # -o arguments set automatically from fstab that should be ignored
                    ignored_values.append(value)
                elif value == "rw":
                    parser.error("Stud.IP FUSE only supports read-only mount")
                else:
                    option_parser.parse_args(["--" + value], namespace)
            if ignored_values:
                logging.debug("Ignoring arguments %s" % ", ".join(ignored_values))

    return anonymous_class

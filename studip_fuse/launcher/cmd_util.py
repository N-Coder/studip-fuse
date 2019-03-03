import argparse as argparse
import logging
import os

import appdirs
from more_itertools import flatten
from yarl import URL

from studip_fuse import __author__ as prog_author, __version__ as prog_version


def parse_args():
    dirs = appdirs.AppDirs("Stud.IP-Fuse", prog_author)

    opts_parser = argparse.ArgumentParser(add_help=False)
    opts_parser.add_argument("-o", help="FUSE-like options", nargs="+", action=StoreNameValuePair(opts_parser))
    opts_parser.add_argument("-d", "--debug", help="turn on all debugging options", action="store_true")
    opts_parser.add_argument("-v", "--debug-logging", help="turn on debug logging", action="store_true")
    opts_parser.add_argument("--debug-aio", help="turn on asyncio debug logging", action="store_true")

    studip_opts = opts_parser.add_argument_group("Stud.IP Driver Options")
    studip_opts.add_argument("--format", help="format specifier for virtual paths",
                             default="{semester-lexical}/{course-class}/{course}/{course-type}/{short-path}/{file-name}")
    studip_opts.add_argument("--cache-dir", "--cache", help="path to cache directory",
                             default=dirs.user_cache_dir)
    studip_opts.add_argument("--studip-url", "--studip", help="Stud.IP API URL", type=URL,
                             default="https://studip.uni-passau.de/studip/api.php/")

    auth_opts = opts_parser.add_argument_group("Authentication Options")
    auth_opts.add_argument("--login-method", help="method for logging in to Stud.IP session",
                           choices=['shib', 'oauth', 'basic'], default="oauth")
    auth_opts.add_argument("--pwfile", help="path to password file or '-' to read from stdin (for 'basic' and 'shib' auth)",
                           default=os.path.join(dirs.user_config_dir, ".studip-pw"))
    auth_opts.add_argument("--shib-url", "--sso", help="Stud.IP SSO URL", type=URL,
                           default="https://studip.uni-passau.de/studip/index.php?again=yes&sso=shib")
    oauth_client_key_default = "[internal key for given Stud.IP instance]"
    auth_opts.add_argument("--oauth-client-key", help="path to JSON file containing OAuth Client Key and Secret",
                           default=oauth_client_key_default)
    auth_opts.add_argument("--oauth-session-token", help="path to file where the session keys should be read from/stored to",
                           default=os.path.join(dirs.user_config_dir, ".studip-oauth-session"))
    auth_opts.add_argument("--oauth-no-login", help="disable interactive OAuth authentication when no valid session token is found",
                           action="store_true")
    auth_opts.add_argument("--oauth-no-browser", help="don't automatically open the browser during interactive OAuth authentication",
                           action="store_true")
    auth_opts.add_argument("--oauth-no-store", help="don't store the new session token obtained after logging in",
                           action="store_true")  # customize oauth timeout?, port, URLs...

    fuse_opts = opts_parser.add_argument_group("FUSE Options")
    fuse_opts.add_argument("-f", "--foreground", help="run in foreground", action="store_true")
    fuse_opts.add_argument("-s", "--nothreads", help="single threads for FUSE", action="store_true")
    fuse_opts.add_argument("--allow-other", help="allow access by all users", action="store_true")
    fuse_opts.add_argument("--allow-root", help="allow access by root", action="store_true")
    fuse_opts.add_argument("--nonempty", help="allow mounts over non-empty file/dir", action="store_true")
    fuse_opts.add_argument("--umask", help="set file permissions (octal)", action="store")
    fuse_opts.add_argument("--uid", help="set file owner", action="store")
    fuse_opts.add_argument("--gid", help="set file group", action="store")
    fuse_opts.add_argument("--default-permissions", help="enable permission checking by kernel", action="store_true")
    fuse_opts.add_argument("--debug-fuse", help="enable FUSE debug mode (includes --foreground)", action="store_true")

    parser = argparse.ArgumentParser(
        description="studip-fuse is a FUSE (file-system in user-space) driver that provides files from lectures in "
                    "the course management tool Stud.IP on your computer.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        parents=[opts_parser])
    parser.add_argument("user", help="Stud.IP username")
    parser.add_argument("mount", help="path to mount point", type=lambda x: os.path.normpath(os.path.expanduser(x)))
    parser.add_argument("-V", "--version", action="version", version="%(prog)s " + prog_version)

    args = parser.parse_args()

    if args.debug:
        args.debug_logging = True
        args.debug_fuse = True
        args.debug_aio = True
    if args.oauth_client_key == oauth_client_key_default:
        args.oauth_client_key = None

    fuse_args = {a.dest: getattr(args, a.dest, None) for a in fuse_opts._group_actions
                 if getattr(args, a.dest, None) is not None}
    return args, fuse_args


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


def get_version():
    import pkg_resources
    import subprocess
    import inspect
    import json

    pkg_data = pkg_resources.require("studip_fuse")[0]
    meta = json.loads(pkg_data.get_metadata('meta.json'))
    install_git_rev = meta.get("install-git-revision", "unknown")
    dirname = os.path.dirname(inspect.getfile(inspect.currentframe()))
    try:
        git_rev = subprocess.check_output(
            ["git", "describe", "--always"],
            cwd=dirname, stderr=subprocess.STDOUT
        ).decode('ascii').strip()
    except (OSError, subprocess.SubprocessError) as e:
        logging.debug("Could not get git revision in install directory %s for package %s", dirname, pkg_data, exc_info=e)
        if isinstance(e, subprocess.CalledProcessError):
            logging.debug("stdout: %s\nstderr: %s", e.stdout, e.stderr)
        git_rev = "release"

    install_notes = []
    if prog_version != pkg_data.version:
        install_notes.append("as version {}".format(pkg_data.version))
    if git_rev != install_git_rev:
        install_notes.append("from revision {}".format(install_git_rev))
    if install_notes:
        install_notes = "(installed {})".format(", ".join(install_notes))
    else:
        install_notes = ""

    return "{} {} {} {}".format(pkg_data.project_name.title(), prog_version, git_rev, install_notes).strip()


def get_environment():
    import platform

    from studip_fuse.launcher.fuse import get_fuse_libfile, get_fuse_version

    return "%s with FUSE %s (%s) running via %s %s on %s" \
           % (get_version(), get_fuse_version(), get_fuse_libfile(),
              platform.python_implementation(), platform.python_version(), platform.platform())

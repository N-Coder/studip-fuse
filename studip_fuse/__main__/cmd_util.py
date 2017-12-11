import argparse as argparse
import asyncio
import logging
import os
import sys
import threading
import traceback

import appdirs
from more_itertools import flatten, one


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


def getpass(args):
    if args.pwfile == "-":
        from getpass import getpass
        return getpass()
    else:
        try:
            with open(args.pwfile) as f:
                return f.read()
        except FileNotFoundError as e:
            logging.warning("%s. Either specifiy a file from which your Stud.IP password can be read "
                            "or use `--pwfile -` to enter it using a promt in the shell." % e)
            raise


thread_log = logging.getLogger("threads")


def await_loop_thread_shutdown(loop, loop_thread):
    # print loop stack trace until the loop thread completed
    counter = 0
    while loop_thread.is_alive() and counter < 4:
        loop_thread.join(5)
        counter += 1
        if loop_thread.is_alive():
            if loop:
                dump_loop_stack(loop)
            else:
                dump_thread_stack(loop_thread)

    if loop_thread.is_alive():
        logging.warning("Shutting down main thread and thus killing hung event loop daemon thread")


def format_stack(stack):
    return "".join(traceback.format_stack(stack[0] if isinstance(stack, list) else stack))


def dump_loop_stack(loop):
    current_task = asyncio.Task.current_task(loop=loop)
    pending_tasks = [t for t in asyncio.Task.all_tasks(loop=loop) if not t.done() and t is not current_task]
    loop_thread = one(t for t in threading.enumerate() if t.ident == loop._thread_id)
    loop_thread_stack = sys._current_frames()[loop_thread.ident]
    loop_thread_stack_trace = format_stack(loop_thread_stack)

    if thread_log.isEnabledFor(logging.DEBUG):
        thread_log.debug("Current task %s in loop %s in loop thread %s", current_task, loop, loop_thread)
        if current_task:
            thread_log.debug("Task stack trace:\n %s", format_stack(current_task.get_stack()))
        thread_log.debug("Thread stack trace:\n %s", loop_thread_stack_trace)
        pending_tasks_str = "\n".join(
            str(t) + "\n" + format_stack(t.get_stack())
            for t in pending_tasks)
        thread_log.debug("%s further pending tasks:\n %s", len(pending_tasks), pending_tasks_str)
    else:
        thread_log.info("Waiting for event loop to stop... (%s further pending tasks after current task %s "
                        "in loop %s in loop thread %s)", len(pending_tasks), current_task, loop, loop_thread)

    if len(pending_tasks) == 0 and current_task is None \
            and "epoll.poll(timeout, max_ev)" in loop_thread_stack_trace.strip().split("\n")[-1]:
        thread_log.warning("Event loop hangs in epoll selector without any tasks pending. "
                           "This is probably a python bug (https://bugs.python.org/issue29780).")


def dump_thread_stack(loop_thread):
    thread_log.info("Waiting for loop thread to abort initialization...")
    if thread_log.isEnabledFor(logging.DEBUG):
        thread_log.debug("Thread stack trace:\n %s", format_stack(sys._current_frames()[loop_thread.ident]))

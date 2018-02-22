import faulthandler
import logging
import logging.config
import sys
import threading

import pkg_resources
import yaml

from studip_fuse.__main__.cmd_util import parse_args
from studip_fuse.__main__.fs_driver import FixedFUSE


def excepthook(type, value, tb):
    logging.error("Uncaught exception:", exc_info=(type, value, tb))


class LoggerWriter:
    def __init__(self, level, old):
        self.level = level
        self.old = old
        self._local = threading.local()

    def write(self, message):
        if getattr(self._local, "writing", False):
            return
        self._local.writing = True
        try:
            try:
                if not self.old.closed:
                    self.old.write(message)
            except AttributeError:
                pass
            message = message.strip()
            if message:
                self.level(message)
        finally:
            self._local.writing = False

    def flush(self):
        if not self.old.closed:
            self.old.flush()


def main():
    try:
        logging.config.dictConfig(yaml.load(pkg_resources.resource_string('studip_fuse.__main__', 'logging.yaml')))
        sys.excepthook = excepthook
        faulthandler.enable(file=sys.stderr, all_threads=True)
        # reroute std streams after logging config, so that a config logging to sys.stdout still logs to the initial stream
        sys.stdout = LoggerWriter(logging.getLogger('studip_fuse.stdout').info, sys.stdout)
        sys.stderr = LoggerWriter(logging.getLogger('studip_fuse.stderr').error, sys.stderr)

        args, http_args, fuse_args = parse_args()

        if not args.debug_logging:
            logging.root.setLevel(logging.INFO)
        if not args.debug_aio:
            logging.getLogger("asyncio").setLevel(logging.WARNING)
        logging.debug("Program started")

        import os
        os.makedirs(args.cache, exist_ok=True)

        from studip_fuse.__main__.fs_driver import FUSEView
        if args.debug_fuse:
            logging.getLogger("studip_fuse.fs_driver.ops").setLevel(logging.DEBUG)
        fuse_ops = FUSEView(args, http_args, fuse_args)

        if args.pwfile == "-":
            from getpass import getpass
            password = getpass()
        else:
            try:
                with open(args.pwfile) as f:
                    password = f.read()
            except FileNotFoundError as e:
                logging.warning("%s. Either specifiy a file from which your Stud.IP password can be read "
                                "or use `--pwfile -` to enter it using a promt in the shell." % e)
                return
        args.get_password = lambda: password  # wrap in lambda to prevent printing

        from fuse import FUSE, fuse_get_context
        logging.debug("Starting FUSE driver to mount at %s (uid=%s, gid=%s, pid=%s, python pid=%s)", args.mount,
                      *fuse_get_context(), os.getpid())
        # This calls fork if args.foreground == False (https://bugs.python.org/issue21998)
        FixedFUSE(fuse_ops, args.mount, debug=fuse_args.pop("debug_fuse"), **fuse_args)
    except SystemExit:
        pass
    except:
        logging.error("main() function quit exceptionally", exc_info=True)
    finally:
        logging.debug("Program terminated")


if __name__ == "__main__":
    main()

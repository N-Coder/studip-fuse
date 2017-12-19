import logging
import logging.config
import sys

import pkg_resources
import yaml


def excepthook(type, value, tb):
    logging.error("Uncaught exception:", exc_info=(type, value, tb))


class LoggerWriter:
    def __init__(self, level, old):
        self.level = level
        self.old = old

    def write(self, message):
        if not self.old.closed:
            self.old.write(message)
        message = message.strip()
        if message:
            self.level(message)

    def flush(self):
        if not self.old.closed:
            self.old.flush()


logging.config.dictConfig(yaml.load(pkg_resources.resource_string('studip_fuse.__main__', 'logging.yaml')))
sys.excepthook = excepthook
# reroute std streams after logging config, so that a config logging to sys.stdout still logs to the initial stream
sys.stdout = LoggerWriter(logging.getLogger('studip_fuse.stdout').info, sys.stdout)
sys.stderr = LoggerWriter(logging.getLogger('studip_fuse.stderr').error, sys.stderr)


def main():
    try:
        from studip_fuse.__main__.cmd_util import parse_args
        args, http_args, fuse_args = parse_args()

        # TODO make control over debug mode and logging more fine granular
        # TODO improve log output in different modi
        if not args.debug:
            logging.root.setLevel(logging.INFO)
            logging.getLogger("sh").setLevel(logging.WARNING)
            logging.getLogger("asyncio").setLevel(logging.WARNING)
        logging.debug("Program started")

        import os
        os.makedirs(args.cache, exist_ok=True)

        from studip_fuse.__main__.fs_driver import LoggingFUSEView, FUSEView
        if args.debug:
            fuse_ops = LoggingFUSEView(args, http_args, fuse_args)
        else:
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
        FUSE(fuse_ops, args.mount, **fuse_args)
    except:
        logging.error("main() function quit exceptionally", exc_info=True)
    finally:
        logging.debug("Program terminated")


if __name__ == "__main__":
    main()

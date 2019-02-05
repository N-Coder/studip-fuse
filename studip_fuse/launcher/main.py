import logging
import logging.config
import os

import studip_fuse.launcher.aioimpl.asyncio as aioimpl_asyncio
from studip_fuse.launcher.cmd_util import parse_args, get_environment
from studip_fuse.launcher.fuse import FUSE, fuse_get_context
from studip_fuse.launcher.log_utils import configure_logging
from studip_fuse.studipfs.fuse_ops import FUSEView, log_status


def main():
    configure_logging()
    args, fuse_args = parse_args()
    try:
        if not args.debug_logging:
            logging.root.setLevel(logging.INFO)
        if not args.debug_aio:
            logging.getLogger("asyncio").setLevel(logging.WARNING)
        logging.debug("Logging started")

        # TODO on windows args.mount may not exist, on Linux it must exist
        os.makedirs(args.cache, exist_ok=True)

        if args.debug_fuse:
            from studip_fuse.studipfs.fuse_ops import log_ops
            log_ops.setLevel(logging.DEBUG)
        fuse_ops = FUSEView(log_args=args, loop_setup_fn=aioimpl_asyncio.setup_loop(args=args))

        if args.pwfile == "-":
            from getpass import getpass
            password = getpass()
        else:
            try:
                with open(args.pwfile) as f:
                    password = f.read().rstrip('\n')
            except FileNotFoundError as e:
                logging.warning("%s. Either specify a file from which your Stud.IP password can be read "
                                "or use `--pwfile -` to enter it using a prompt in the shell." % e)
                return
        args.get_password = lambda: password  # wrap in lambda to prevent printing

        log_status("STARTING", args=args, level=logging.DEBUG)
        logging.info("Starting %s" % get_environment())
        logging.debug("Going to mount at %s (uid=%s, gid=%s, pid=%s, python pid=%s)", args.mount,
                      *fuse_get_context(), os.getpid())
        # This calls fork if args.foreground == False (https://bugs.python.org/issue21998)
        FUSE(fuse_ops, args.mount, debug=fuse_args.pop("debug_fuse"), **fuse_args)
    except SystemExit:
        pass
    except:
        logging.error("main() function quit exceptionally", exc_info=True)
    finally:
        log_status("TERMINATED", args=args, level=logging.DEBUG)
        logging.debug("Program terminated")


if __name__ == "__main__":
    main()

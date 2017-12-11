import logging
import logging.handlers
import warnings


def main():
    from studip_fuse.__main__.cmd_util import parse_args
    args, http_args, fuse_args = parse_args()

    # TODO make control over debug mode and logging more fine granular
    # TODO improve log output in different modi
    if args.debug:
        logging.root.setLevel(logging.DEBUG)
        warnings.resetwarnings()
    else:
        logging.root.setLevel(logging.INFO)
        logging.getLogger("sh").setLevel(logging.WARNING)
        logging.getLogger("asyncio").setLevel(logging.WARNING)

    import os
    os.makedirs(args.cache, exist_ok=True)

    from studip_fuse.__main__.fs_driver import LoggingFUSEView, FUSEView
    if args.debug:
        fuse_ops = LoggingFUSEView(args, http_args, fuse_args)
    else:
        fuse_ops = FUSEView(args, http_args, fuse_args)

    from fuse import FUSE, fuse_get_context
    logging.info("Starting FUSE driver to mount at %s (uid=%s, gid=%s, pid=%s, python pid=%s)", args.mount,
                 *fuse_get_context(), os.getpid())
    FUSE(fuse_ops, args.mount, **fuse_args)


if __name__ == "__main__":
    try:
        main()
    except:
        logging.error("main() function quit exceptionally", exc_info=True)
        raise
    finally:
        logging.info("Terminated")

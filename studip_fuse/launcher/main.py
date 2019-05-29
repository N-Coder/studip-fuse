import logging
import logging.config
import os

import appdirs
import more_itertools

import studip_fuse.launcher.aioimpl.asyncio as aioimpl_asyncio
from studip_fuse.launcher.cmd_util import get_environment, parse_args
from studip_fuse.launcher.fuse import FUSE, fuse_get_context
from studip_fuse.launcher.log_utils import configure_logging
from studip_fuse.studipfs.fuse_ops import FUSEView, log_status

log = logging.getLogger(__name__)

FUSE_ERROR_CODES = {  # https://libfuse.github.io/doxygen/fuse_8h.html#ac99b844cee7aaa8fb4e35df5b5488d82
    1: "Invalid option arguments",
    2: "No mount point specified",
    3: "FUSE setup failed",
    4: "Mounting failed",
    5: "Failed to daemonize (detach from session)",
    6: "Failed to set up signal handlers",
    7: "An error occured during the life of the file system",
}


def login_oauth_args(args):
    def oauth_data_from_file(path):
        if not path:
            return {}
        try:
            if os.path.getsize(path) == 0:
                return {}
        except OSError:
            return {}

        with open(path, "rt") as f:
            import json
            return json.load(f)

    def start_oauth_login_browser(url):
        log_status("OAUTH1", args=args, suffix=(url,), level=logging.DEBUG)
        if not args.oauth_no_browser:
            import webbrowser
            webbrowser.open(url)

    from oauthlib.oauth1 import Client as OAuth1Client
    from studip_fuse.launcher.aioimpl.asyncio.oauth import get_tokens

    if args.oauth_client_key:
        oauth_client = OAuth1Client(**oauth_data_from_file(args.oauth_client_key), **oauth_data_from_file(args.oauth_session_token))
    else:
        oauth_client = OAuth1Client(*get_tokens(args.studip_url), **oauth_data_from_file(args.oauth_session_token))

    if not args.oauth_no_login:
        import asyncio
        from studip_fuse.launcher.aioimpl.asyncio.oauth import obtain_access_token_sessionless
        coro = obtain_access_token_sessionless(oauth_client, studip_url=args.studip_url, open_browser=start_oauth_login_browser)
        loop = asyncio.get_event_loop()
        try:
            oauth_client = loop.run_until_complete(coro)
        finally:
            loop.close()

    if not args.oauth_no_store:
        with open(args.oauth_session_token, "wt") as f:
            import json
            json.dump({
                "resource_owner_key": oauth_client.resource_owner_key,
                "resource_owner_secret": oauth_client.resource_owner_secret
            }, f)

    args.get_oauth_args = lambda: oauth_client.__dict__


def main(argv=None):
    dirs = appdirs.AppDirs(appname="Stud.IP-Fuse", appauthor=False)  # disable author/company folder on windows
    os.makedirs(dirs.user_data_dir, exist_ok=True)  # must exist for log files
    os.makedirs(dirs.user_config_dir, exist_ok=True)  # must exist for oauth token storage
    configure_logging(dirs)
    args, fuse_args = parse_args(dirs, argv)
    os.makedirs(args.cache_dir, exist_ok=True)
    try:
        if not args.debug_logging:
            logging.root.setLevel(logging.INFO)
        if not args.debug_aio:
            logging.getLogger("asyncio").setLevel(logging.WARNING)

        log_status("STARTING", args=args, level=logging.DEBUG)
        log.info("Starting %s" % get_environment())

        if args.debug_fuse:
            from studip_fuse.studipfs.fuse_ops import log_ops
            log_ops.setLevel(logging.DEBUG)
        fuse_ops = FUSEView(log_args=args, loop_setup_fn=aioimpl_asyncio.setup_loop(args=args))

        if args.login_method == "oauth":
            login_oauth_args(args)
        else:
            if args.pwfile == "-":
                from getpass import getpass
                password = getpass()
            else:
                try:
                    with open(args.pwfile, "rt") as f:
                        password = f.read().rstrip('\n')
                except FileNotFoundError as e:
                    log.warning("%s. Either specify a file from which your Stud.IP password can be read "
                                "or use `--pwfile -` to enter it using a prompt in the shell." % e)
                    return
            args.get_password = lambda: password  # wrap in lambda to prevent printing

        log.debug("Going to mount at %s (uid=%s, gid=%s, pid=%s, python pid=%s)", os.path.abspath(args.mount),
                  *fuse_get_context(), os.getpid())
        try:
            # this calls fork if args.foreground == False (and breaks running asyncio loops due to https://bugs.python.org/issue21998)
            # XXX on windows args.mount may not exist, on Linux it must exist
            FUSE(fuse_ops, args.mount, debug=fuse_args.pop("debug_fuse"), **fuse_args)
        except RuntimeError as e:
            if more_itertools.first(e.args, None) in FUSE_ERROR_CODES:
                msg = FUSE_ERROR_CODES[e.args[0]]
                if e.args[0] == 1:
                    msg += ". Please check whether the mountpoint you specified is an empty directory or another instance of studip-fuse is using it"
                msg += ". Please check stderr for details."
                raise RuntimeError(msg) from e
            else:
                raise
    except SystemExit:
        pass
    except:
        log.error("main() function quit exceptionally", exc_info=True)
    finally:
        log_status("TERMINATED", args=args, level=logging.DEBUG)
        log.debug("Program terminated")


if __name__ == "__main__":
    main()

import faulthandler
import logging
import logging.config
import os
import sys
import threading

import appdirs
import pkg_resources
import yaml


def excepthook(ex_type, value, tb):
    logging.getLogger(__name__).error("Uncaught exception:", exc_info=(ex_type, value, tb))


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


def configure_logging():
    dirs = appdirs.AppDirs("Stud.IP-Fuse", False)
    os.makedirs(dirs.user_data_dir, exist_ok=True)

    logging_path = os.path.join(dirs.user_config_dir, "studip-logging-config.yaml")
    if os.path.isfile(logging_path):
        with open(logging_path, "rb") as f:
            logging_config = yaml.load(f)
    else:
        # will only work if studip_fuse.launcher has an __init__.py file
        logging_config = yaml.safe_load(pkg_resources.resource_string("studip_fuse.launcher", "logging.yaml"))

    if "handlers" in logging_config:
        handlers = logging_config["handlers"]

        if "syslog" in handlers and "address" not in handlers["syslog"]:
            if os.path.exists("/dev/log"):
                # probably Linux syslog
                handlers["syslog"]["address"] = "/dev/log"
            elif os.path.exists("/var/run/syslog"):
                # probably Mac OS syslog
                handlers["syslog"]["address"] = "/var/run/syslog"
            else:
                # syslog not available (e.g. Windows), disable
                handlers["syslog"] = {"class": "logging.NullHandler"}

        if "file" in handlers and "filename" not in handlers["file"]:
            handlers["file"]["filename"] = os.path.join(dirs.user_data_dir, "studip-log.txt")

        if "status" in handlers and "filename" not in handlers["status"]:
            handlers["status"]["filename"] = os.path.join(dirs.user_data_dir, "studip-status.txt")

    logging.config.dictConfig(logging_config)

    sys.excepthook = excepthook
    faulthandler.enable(file=open(os.path.join(dirs.user_data_dir, "studip-fault-tb.txt"), "wt"), all_threads=True)
    # reroute std streams after logging config, so that a config logging to sys.stdout still logs to the initial stream
    sys.stdout = LoggerWriter(logging.getLogger('studip_fuse.stdout').info, sys.stdout)
    sys.stderr = LoggerWriter(logging.getLogger('studip_fuse.stderr').error, sys.stderr)

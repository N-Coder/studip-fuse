from flask import Flask

from studip_fuse.__main__.fs_driver import FUSEView

app = Flask("studip_fuse.http")
fuseview_inst = None  # type: FUSEView


def run(fuseview):
    global fuseview_inst
    fuseview_inst = fuseview
    app.run()


@app.route("/show_caches")
def show_caches():
    from studip_fuse.cache import AsyncTaskCache
    return AsyncTaskCache.format_all_statistics()


@app.route("/clear_caches", methods=["POST"])
def clear_caches():
    from studip_fuse.cache import AsyncTaskCache
    import asyncio
    return asyncio.run_coroutine_threadsafe(AsyncTaskCache.clear_all_caches(), fuseview_inst.loop).result()


@app.route("/save_model", methods=["POST"])
def save_model():
    return fuseview_inst.session.save_model()


@app.route("/load_model", methods=["POST"])
def load_model():
    return fuseview_inst.session.load_model()

    # def getxattr(self, path, name, position=0):
    #     status = [
    #         "unknown", # == "unavailable-offline",
    #         "pending",
    #         "available",
    #         "stale",
    #         "failed",
    #     ]
    #     return {"studip-fuse.contents-status":status}
    #
    # def listxattr(self, path):
    #     return ["studip-fuse.contents-status"]

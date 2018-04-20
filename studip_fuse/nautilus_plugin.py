def main():
    import inspect
    import os

    import appdirs

    print("Checking requirements...")
    try:
        import gi

        gi.require_version('Nautilus', '3.0')
        from gi.repository import Nautilus, GObject, Gio
    except:
        print("Could not import Nautilus 3.0, GObject or Gio from gi repository. ")

    folder = os.path.join(appdirs.user_data_dir("nautilus-python"), "extensions")
    os.makedirs(folder, exist_ok=True)
    print("Installing studip-fuse Nautilus extension to %s..." % folder)

    script_file = inspect.getfile(inspect.currentframe())
    dest_file = os.path.join(folder, "studip_fuse_nautilus_plugin.py")
    if os.path.isfile(script_file):
        print("Creating symbolic link from %s to %s..." % (script_file, dest_file))
        if os.path.isfile(dest_file):
            print("Removing previous link...")
            os.remove(dest_file)
        os.symlink(script_file, dest_file)
        print("Link created.")
    else:
        print("Copying script source code to %s..." % dest_file)
        with open(dest_file, "wt") as f:
            lines, lineno = inspect.getsourcelines(inspect.currentframe())
            f.writelines(lines)
            print("Wrote %s lines." % len(lines))

    print("Done installing, please restart Nautilus to enable the plugin.")
    # use `NAUTILUS_PYTHON_DEBUG=misc nautilus` for debugging


if __name__ == "__main__":
    main()
else:
    import gi

    gi.require_version('Nautilus', '3.0')
    from gi.repository import Nautilus, GObject, Gio


    class InfoProvider(GObject.GObject, Nautilus.InfoProvider):
        def update_file_info(self, file):
            gfile = file.get_location()
            xattr_info = gfile.query_info("xattr::*", Gio.FileQueryInfoFlags.NONE, None)
            status = xattr_info.get_attribute_string("xattr::studip-fuse.contents-status")
            emblem = {
                "unknown": "new",
                "pending": "synchronizing",
                "failed": "unreadable",
                "available": "default",
            }.get(status, None)
            # TODO mark as unreadable if offline, update emblems on change
            if emblem:
                file.add_emblem(emblem)

# TODO add appindicator and notifications, error reporting
# http://candidtim.github.io/appindicator/2014/09/13/ubuntu-appindicator-step-by-step.html
# https://lazka.github.io/pgi-docs/#Notify-0.7/classes/Notification.html

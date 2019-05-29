import os
import sys


def make_shortcut(cmd, args):
    import win32com.client

    shell = win32com.client.Dispatch("WScript.Shell")
    desktop_dir = shell.SpecialFolders("Desktop")
    shortcut = shell.CreateShortcut(os.path.join(desktop_dir, "Stud.IP FUSE.lnk"))
    shortcut.TargetPath = cmd
    shortcut.Arguments = args
    shortcut.WorkingDirectory = os.path.expanduser("~")
    shortcut.Save()


def get_executable_location():
    import site
    from shutil import which

    pythonpath = os.path.dirname(os.path.normpath(sys.executable))
    scripts = os.path.join(pythonpath, "Scripts")
    appdata = os.environ["APPDATA"]
    envpath = os.environ["HOME"].split(os.pathsep)
    if hasattr(site, "USER_SITE"):
        userpath = site.USER_SITE.replace(appdata, "%APPDATA%")
        userscripts = os.path.join(userpath, "Scripts")
    else:
        userscripts = None

    pypaths = set(path for path in envpath + [pythonpath, scripts, userscripts]
                  if path and os.path.isdir(path))
    pypath = os.pathsep.join(pypaths)

    actual_path = which("studip-fuse")
    expected_path = which("studip-fuse", path=pypath)
    if actual_path != expected_path:
        print("studip-fuse was not found in PATH, please run %s to fix this",
              os.path.join(pythonpath, "Tools", "Scripts", "win_add2path.py"))

    return expected_path

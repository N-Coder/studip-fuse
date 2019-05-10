def make_shortcut(args):
    import os
    import win32com.client

    shell = win32com.client.Dispatch("WScript.Shell")
    desktop_dir = shell.SpecialFolders("Desktop")
    shortcut = shell.CreateShortcut(os.path.join(desktop_dir, "Stud.IP FUSE.lnk"))
    shortcut.TargetPath = "C:\Windows\System32\cmd.exe"
    shortcut.Arguments = "/k studip-fuse " + args
    shortcut.WorkingDirectory = os.path.expanduser("~")
    shortcut.Save()

# Stud.IP FUSE driver

_studip-fuse_ is a FUSE (file-system in user-space) driver that provides files from lectures in the course management tool Stud.IP on your computer.

_studip-fuse_ uses the official Stud.IP REST API, but still needs your username and password to log in via the standard Stud.IP login (`--login-method basic`) or Shibboleth (`--login-method shib`).
All connections to the university servers transporting the login data are made via HTTPS.
Your credentials will not be copied or distributed in any other way, passwordless OAuth login is currently work in progress.

This program has been tested on
- Ubuntu 16.04 using Python 3.5 and pip 8.1.1
- Fedora 26    using Python 3.6 and pip 9.0.1

using the Stud.IP instance of University of Passau (https://uni-passau.de/).


# Installation (on Ubuntu 16.04)

Install the debian packages `python3` (providing binary `python3` version 3.5) and `python3-pip` (providing binary `pip3` version 8.1.1) on your system via apt:
```
$ sudo apt install python3 python3-pip
```
And install the python package studip-fuse from GitHub for your current user.
```
$ pip3 install git+https://github.com/N-Coder/studip-fuse --user
```

Now you can try mounting your Stud.IP files:
```
mkdir Stud.IP
studip-fuse mueller123 ~/Stud.IP --pwfile=- --foreground
```

To mount the drive you can also use the standard `mount` tool if you installed `studip-fuse` globally:
```
$ sudo -i
# pip3 install git+https://github.com/N-Coder/studip-fuse
# mount -t fuse -o allow_other,uid=1000,gid=1000 "studip-fuse#mueller123" /home/user/Stud.IP
```

If you're running the driver in the background (i.e. by not passing the `--foreground` option), all status messages (e.g. warnings about an invalid password) will be sent to the system log running under `/dev/log`.
You can unmount the folder and kill the driver the usual way:
```
# umount /home/user/Stud.IP # or, alternatively also with user rights:
$ fusermount -u ~/Stud.IP
```

# Command-line options
```
$ studip-fuse -h
usage: studip-fuse [-h] [-o O [O ...]] [-d] [-v] [--debug-aio] [--pwfile PWFILE] [--format FORMAT] [--cache CACHE]
                   [--login-method {shib,oauth,basic}] [--studip STUDIP] [--sso SSO] [-f] [-s] [--allow-other]
                   [--allow-root] [--nonempty] [--umask UMASK] [--uid UID] [--gid GID] [--default-permissions]
                   [--debug-fuse] [-V]
                   user mount

studip-fuse is a FUSE (file-system in user-space) driver that provides files from lectures in the course management
tool Stud.IP on your computer.

positional arguments:
  user                  Stud.IP username
  mount                 path to mount point

optional arguments:
  -h, --help            show this help message and exit
  -o O [O ...]          FUSE-like options (default: None)
  -d, --debug           turn on all debugging options (default: False)
  -v, --debug-logging   turn on debug logging (default: False)
  --debug-aio           turn on asyncio debug logging (default: False)
  -V, --version         show program's version number and exit

Stud.IP Driver Options:
  --pwfile PWFILE       path to password file or '-' to read from stdin
                        (default: /home/user/.config/Stud.IP-Fuse/.studip-pw)
  --format FORMAT       format specifier for virtual paths
                        (default: {semester-lexical}/{course-class}/{course}/{course-type}/{short-path}/{file-name})
  --cache CACHE         path to cache directory (default: /home/user/.cache/Stud.IP-Fuse)
  --login-method {shib,oauth,basic}
                        method for logging in to Stud.IP session (default: shib)
  --studip STUDIP       Stud.IP API URL (default: https://studip.uni-passau.de/studip/api.php/)
  --sso SSO             Studi.IP SSO URL (default: https://studip.uni-passau.de/studip/index.php?again=yes&sso=shib)

FUSE Options:
  -f, --foreground      run in foreground (default: False)
  -s, --nothreads       single threads for FUSE (default: False)
  --allow-other         allow access by all users (default: False)
  --allow-root          allow access by root (default: False)
  --nonempty            allow mounts over non-empty file/dir (default: False)
  --umask UMASK         set file permissions (octal) (default: None)
  --uid UID             set file owner (default: None)
  --gid GID             set file group (default: None)
  --default-permissions
                        enable permission checking by kernel (default: False)
  --debug-fuse          enable FUSE debug mode (includes --foreground) (default: False)

```

## Option format
Options can either be specified using `--key=value` or `-o key=value`, so the following to lines are identical regarding the option values:
```
studip-fuse mueller123 /home/user/Stud.IP --allow_root --uid=1000 --gid=1000
mount -t fuse -o allow_root,uid=1000,gid=1000 "studip-fuse#mueller123" /home/user/Stud.IP
```

## Path formatting options
You can use the following values in the format string for the generated paths:
<!-- TODO update once format keys are stable -->
<dl>
    <dt>semester</dt>
    <dd>semester name in the format "WS 17/18"</dd>
    <dt>semester-lexical</dt>
    <dd>semester name in the format "2017WS18"</dd>
    <dt>semester-lexical-short</dt>
    <dd>semester name in the format "2017WS"</dd>
    <dt>course</dt>
    <dd>the full name of the course</dd>
    <dt>course-abbrev</dt>
    <dd>an abbreviation of the course name using its initials</dd>
    <dt>course-id</dt>
    <dd>the UUID of the course</dd>
    <dt>type</dt>
    <dd>the type of the course (Vorlesung, Uebung, Seminar, ...)</dd>
    <dt>type-abbrev</dt>
    <dd>an abbreviation of the course type using its initials (V, U, S,...)</dd>
    <dt>path</dt>
    <dd>the full path to the file in the course</dd>
    <dt>short-path</dt>
    <dd>the path to the file in the course, without generic prefixes like "Allgemeiner Dateiordner"</dd>
    <dt>id</dt>
    <dd>the UUID of the file</dd>
    <dt>name</dt>
    <dd>the filename, including its extension</dd>
    <dt>description</dt>
    <dd>the description of the file</dd>
    <dt>author</dt>
    <dd>the author of the file</dd>
    <dt>created</dt>
    <dd>timestamp when the file was created</dd>
    <dt>changed</dt>
    <dd>timestamp when the file was last changed</dd>
</dl>

You can combine these formatting options in any way you like, e.g.:
```
studip-fuse mueller123 ~/Stud.IP --format="{semester-lexical-short}/{course-abbrev} {type-abbrev}/{short-path}/{author} - {name}"
```
Not all combinations have been tested, if you encounter any problems with a (sensible) combination, please open a bug report.
Please note that depending on your path format, generating folder listings could become very slow.
For example using the format "{course}/{semester-lexical-short} {type-abbrev}/{short-path}/{name}" would require listing all your courses from all your semesters, which might take a while.

# Modes of Operation
This driver obeys the [Unix philosophy](https://en.wikipedia.org/wiki/Unix_philosophy) of doing one thing well and working together with other programs. Advanced features, for which generic solutions already exists, haven't been implemented redundantly to keep the program simple.

For this reason, the following details are design-decisions and no bugs:
- the whole mount is read-only, so no modification to the downloaded files is possible.
- all information is cached in a static, but lazy way. This means that once you open a folder or file, its contents will be loaded once and stay the same throughout the whole lifetime of the program, even if they are changed online.
  To load the updated information, you need to restart the driver. <!-- or open a file descriptor for the hidden file ".clear_caches" (e.g. using `cat Stud.IP/.clear_caches`). -->
  Note: If a file didn't change online, its previously downloaded version may still be reused. Directories will always be loaded anew.
- the driver needs to have a working internet connection to load new directories or files. Making already loaded files and folder persistently available when working offline is not guaranteed.
- when mounting in background mode, problems with the Stud.IP API (e.g. wrong password) will only be detected _after_ the program forks to background. This problem will be reported to the syslog and the program exits.

...but there are existing tools that fix this peculiarities:
- to keep all files available for offline use, just use a standard synchronization tool like rsync ([example config](https://gist.github.com/n-st/b77e45895da58d99d381b9f97e3c3ad6)) or syncthing (with the driver running on your server).
- to update information that has changed online, mount studip-fuse via autofs, so that it will be unmounted automatically once you don't need it anymore. Once you access the folder again, the driver will be started anew and load the new information.
- to make local changes to the files, use overlayfs to make the read-only studip-fuse filesystem writeable by storing the changes in a separate location ([example config](https://gist.github.com/N-Coder/d5ec5356a12d8ee7a9069188e15f75ce)). This also enables you to delete (i.e. hide) and rename files and folders, while renamed entities will still update their contents when they are changed online.
- to wait for successful startup, check the file `studip-status.txt` in the `user_data_dir`, which will be appended once the driver completed starting up. See [here](https://github.com/N-Coder/studip-fuse/issues/11) on how to use use `tail` and `grep` for this.

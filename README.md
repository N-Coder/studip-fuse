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

To display file status information emblems and add an "Open on Stud.IP" option menu entry in Nautilus, run the following command to install the plug-in:
```
$ studip-fuse-install-nautilus-plugin                                                                                                                            [dev!][0][15:23]
Checking requirements...
Installing studip-fuse Nautilus extension to /home/niko/.local/share/nautilus-python/extensions...
Copying script source code to /home/niko/.local/share/nautilus-python/extensions/studip_fuse_nautilus_plugin.py...
Done installing, please restart Nautilus to enable the plugin.
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

<dl>
<dt>path</dt>
<dd>path to the file, relative to the root folder of the course<br/><i>Example: Hauptordner/Skript/PDF-Versionen</i></dd>
<dt>short-path</dt>
<dd>path to the file, relative to the root folder of the course, stripped from common parts<br/><i>Example: Skript/PDF-Versionen</i></dd>
<dt>semester</dt>
<dd>full semester name<br/><i>Example: Wintersemester 2018-2019</i></dd>
<dt>semester-id</dt>
<dd>system-internal hexadecimal UUID of the semester<br/><i>Example: 36bd96b432c1169978c1594d2251e629</i></dd>
<dt>semester-lexical</dt>
<dd>full semester name, allowing alphabetic sorting<br/><i>Example: 2018 WS -19</i></dd>
<dt>semester-lexical-short</dt>
<dd>shortened semester name, allowing alphabetic sorting<br/><i>Example: 2018WS</i></dd>
<dt>semester-short</dt>
<dd>shortened semester name<br/><i>Example: WS 18-19</i></dd>
<dt>course</dt>
<dd>official name of the course, usually excluding its type<br/><i>Example: Algorithmen und Datenstrukturen</i></dd>
<dt>course-abbrev</dt>
<dd>abbreviation of the course name, generated from its initials<br/><i>Example: AuD</i></dd>
<dt>course-class</dt>
<dd>type of the course (teaching, community,...)<br/><i>Example: Lehre</i></dd>
<dt>course-description</dt>
<dd>optional description given for the course</dd>
<dt>course-group</dt>
<dd>user-assigned (color-)group of the course on the Stud.IP overview page<br/><i>Example: 7</i></dd>
<dt>course-id</dt>
<dd>system-internal hexadecimal UUID of the course<br/><i>Example: eceaf9871792e0339797d1be91f9015d</i></dd>
<dt>course-location</dt>
<dd>room where the course is held</dd>
<dt>course-number</dt>
<dd>number assigned to the course in the course catalogue<br/><i>Example: 5200</i></dd>
<dt>course-subtitle</dt>
<dd>optional subtitle assigned to the course</dd>
<dt>course-type</dt>
<dd>type of the course (lecture, exercise,...)<br/><i>Example: Vorlesung</i></dd>
<dt>course-type-short</dt>
<dd>abbreviated type of the course, usually the letter appended to the course number in the course catalogue<br/><i>Example: V</i></dd>
<!--<dt>file-author</dt>
<dd>the person that uploaded this file<br/><i>Example: Prof. Dr. Franz Brandenburg </i></dd>-->
<dt>file-description</dt>
<dd>optional description given for the file<br/><i>Example: Kapitel 1</i></dd>
<dt>file-downloads</dt>
<dd>number of times the file has been downloaded<br/><i>Example: 3095</i></dd>
<dt>file-id</dt>
<dd>system-internal hexadecimal UUID of the file<br/><i>Example: 8556e68de68b5e33d8d4572057431233</i></dd>
<dt>file-mime-type</dt>
<dd>file's mime-type detected by Stud.IP<br/><i>Example: application-pdf</i></dd>
<dt>file-name</dt>
<dd>(base-)name of the file, including its extension<br/><i>Example: A+D141.pdf</i></dd>
<dt>file-size</dt>
<dd>file size in bytes<br/><i>Example: 3666701</i></dd>
<dt>file-storage</dt>
<dd>how the file is stored on the Stud.IP server<br/><i>Example: disk</i></dd>
<dt>file-terms</dt>
<dd>terms on which the file might be used<br/><i>Example: SELFMADE_NONPUB</i></dd>
</dl>

You can combine these formatting options in any way you like, e.g.:
```
studip-fuse mueller123 ~/Stud.IP --format="{semester-lexical-short}/{course-abbrev} {type-abbrev}/{short-path}/{author} - {name}"
```
Not all combinations have been tested, if you encounter any problems with a (sensible) combination, please open a bug report.
Please note that depending on your path format, generating folder listings could become very slow.
For example using the format "{course}/{semester-lexical-short} {type-abbrev}/{short-path}/{name}" would require listing all your courses from all your semesters, which might take a while.

### Further information on files

To get more information on the files in your Stud.IP folder, have a look at their xargs:
```
$ attr -l '~/Stud.IP/2014 SS/Lehre/Algorithmen und Datenstrukturen/Vorlesung/Skript/PDF-Versionen/A+D141.pdf'
```
The following keys are available:
<dl>
<dt>"studip-fuse.known-tokens"</dt>
<dd>JSON-object with all the tokens mentioned above and their corresponding values for the respective file</dd>
<dt>"studip-fuse.json"</dt>
<dd>big JSON-object with *all* the information we got from the Stud.IP REST API</dd>
<dt>"studip-fuse.contents-status"</dt>
<dd>string indicating whether the contents of the file or folder are available yet:<br/>pending, available, failed, unknown or unavailable</dd>
<dt>"studip-fuse.contents-excpetion"</dt>
<dd>exception that prevented the contents of the file or folder from being loaded</dd>
<dt>"studip-fuse.url"</dt>
<dd>absolute URL to the object in the Stud.IP web interface</dd>
</dl>

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

# Stud.IP FUSE driver

_studip-fuse_ is a FUSE (file-system in user-space) driver that provides files from lectures in the course management tool Stud.IP on your computer.

_studip-fuse_ uses the official Stud.IP REST API and authenticates via OAuth1, which will open a prompt in your browser on the first start.
Password-based login via the standard Stud.IP login (using HTTP Basic Auth `--login-method basic`) or Shibboleth (`--login-method shib`) is also possible.
All connections to the university servers transporting the login data are made via HTTPS and your credentials will not be copied or distributed in any other way.

# Installation (on Ubuntu 18.04)

Install the debian packages `python3` (providing binary `python3` version &geq; 3.6), `python3-pip` (providing binary `pip3` version &geq; 9) and `fuse` on your system via apt:
```bash
sudo apt install python3 python3-pip fuse
```
And install the python package studip-fuse from GitHub for your current user.
```bash
pip3 install git+https://github.com/N-Coder/studip-fuse --user
```

Now you can try mounting your Stud.IP files (optionally pointing `--studip-url` to the `api.php` endpoint of your Stud.IP instance and providing the appropriate [`--login-method`](#Login-Methods)):
```bash
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
$ studip-fuse-install-nautilus-plugin
Checking requirements...
Installing studip-fuse Nautilus extension to /home/user/.local/share/nautilus-python/extensions...
Copying script source code to /home/user/.local/share/nautilus-python/extensions/studip_fuse_nautilus_plugin.py...
Done installing, please restart Nautilus to enable the plugin.
```

# Supported Environments

This program has been tested on
- Ubuntu 18.04 using Python 3.6.7 and pip 9.0.1
- Fedora 29    using Python 3.7.2 and pip 18.1

using the Stud.IP instance of University of Passau (https://uni-passau.de/studip) and the Stud.IP Developer Instance (http://develop.studip.de/studip/).

Ubuntu 16.04 is no longer officially supported, because it [only ships python 3.5.2](https://packages.ubuntu.com/xenial-updates/python3.5), but [aiohttp requires &geq; 3.5.3](https://github.com/aio-libs/aiohttp/blob/master/docs/faq.rst#why-is-python-3-5-3-the-lowest-supported-version).

## Login Methods

Currently, three different methods for logging in to Stud.IP are supported via `--login-method`:
<dl>
<dt>`basic`</dt>
<dd>This can be used if your Stud.IP instance uses the built-in authentication system.
Use this, if your login page looks like <a href="https://develop.studip.de/studip/index.php?again=yes">this</a> 
and/or the Basic Auth dialog popping up when you open the `api.php` URL accepts your credentials.</dd>
<dt>`shib`</dt>
<dd>This can be used if your Stud.IP instance uses Shibboleth Single Sign-On.
Use this, if your login page looks like <a href="https://studip.uni-passau.de/studip/index.php?again=yes&sso=shib">this</a>.
Please note that this login method parses the HTML responses of the Shibboleth server that corresponds to `--shib-url`, so things might break.
<dt>`oauth`</dt>
<dd>This should be available for any Stud.IP instance, as long as you registered an OAuth application 
with the administrator of the instance and provide the appropriate `--oauth-client-key`, 
or your instance provided built-in client keys in <a href="studip_fuse/launcher/oauth_tokens.py">studip_fuse/launcher/oauth_tokens.py</a>.</dd>
</dl>

## Required Routes

The following routes need to be available from the API defined by `--studip-url`:

- `discovery`,
- `user`,
- `studip/settings`,
- `studip/content_terms_of_use_list`,
- `studip/file_system/folder_types`,
- `extern/coursetypes`,
- `semesters`,
- `user/:user_id/courses`,
- `course/:course_id/top_folder`,
- `folder/:folder_id`,
- `file/:file_ref_id`,
- `file/:file_ref_id/download`

The list is also checked at every startup, see [REQUIRED_API_ENDPOINTS in studip_fuse/studipfs/api/session.py](studip_fuse/studipfs/api/session.py).
If any of the routes is not available and a HTTP error 403 "route not activated" is returned, please contact the administrators of your Stud.IP instance.
    
## Installation Options

Any of the following commands should work interchangeably for installing Stud.IP-FUSE
```bash
pip3 install --user git+https://github.com/N-Coder/studip-fuse
pip3 install --user --editable git+https://github.com/N-Coder/studip-fuse
```
or after a `git clone` of this repository and `cd`ing to that directory
```bash
pip3 install --user .
pip3 install --user --editable .
python3 ./setup.py install --user
python3 ./setup.py develop --user
```
If you think anything regarding your installation broke, try running `pip3 uninstall studip-fuse` 
until the package is no longer found and then reinstalling.


# Command-line options
```
$ studip-fuse -h
usage: studip-fuse [-h] [-o O [O ...]] [-d] [-v] [--debug-aio] [--format FORMAT] [--cache-dir CACHE_DIR]
                   [--studip-url STUDIP_URL] [--login-method {shib,oauth,basic}] [--pwfile PWFILE] [--shib-url SHIB_URL]
                   [--oauth-client-key OAUTH_CLIENT_KEY] [--oauth-session-token OAUTH_SESSION_TOKEN] [--oauth-no-login]
                   [--oauth-no-browser] [--oauth-no-store] [-f] [-s] [--allow-other] [--allow-root] [--nonempty]
                   [--umask UMASK] [--uid UID] [--gid GID] [--default-permissions] [--debug-fuse] [-V]
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
  --format FORMAT       format specifier for virtual paths
                        (default: {semester-lexical}/{course-class}/{course}/{course-type}/{short-path}/{file-name})
  --cache-dir CACHE_DIR, --cache CACHE_DIR
                        path to cache directory (default: /home/user/.cache/Stud.IP-Fuse)
  --studip-url STUDIP_URL, --studip STUDIP_URL
                        Stud.IP API URL (default: https://studip.uni-passau.de/studip/api.php/)

Authentication Options:
  --login-method {shib,oauth,basic}
                        method for logging in to Stud.IP session (default: oauth)
  --pwfile PWFILE       path to password file or '-' to read from stdin (for 'basic' and 'shib' auth)
                        (default: /home/user/.config/Stud.IP-Fuse/.studip-pw)
  --shib-url SHIB_URL, --sso SHIB_URL
                        Stud.IP SSO URL (default: https://studip.uni-passau.de/studip/index.php?again=yes&sso=shib)
  --oauth-client-key OAUTH_CLIENT_KEY
                        path to JSON file containing OAuth Client Key and Secret
                        (default: [internal key for given Stud.IP instance])
  --oauth-session-token OAUTH_SESSION_TOKEN
                        path to file where the session keys should be read from/stored to
                        (default: /home/user/.config/Stud.IP-Fuse/.studip-oauth-session)
  --oauth-no-login      disable interactive OAuth authentication when no valid session token is found (default: False)
  --oauth-no-browser    don't automatically open the browser during interactive OAuth authentication (default: False)
  --oauth-no-store      don't store the new session token obtained after logging in (default: False)

FUSE Options:
  -f, --foreground      run in foreground (default: False)
  -s, --nothreads       single threads for FUSE (default: False)
  --allow-other         allow access by all users (default: False)
  --allow-root          allow access by root (default: False)
  --nonempty            allow mounts over non-empty file/dir (default: False)
  --umask UMASK         set file permissions (octal) (default: None)
  --uid UID             set file owner (default: None)
  --gid GID             set file group (default: None)
  --default-permissions enable permission checking by kernel (default: False)
  --debug-fuse          enable FUSE debug mode (includes --foreground) (default: False)

```

## Option format
Options can either be specified using `--key=value` or `-o key=value`, so the following to lines are identical regarding the option values:
```bash
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

# Alternative Implementations

- [fknorr/studip-client](https://github.com/fknorr/studip-client), [N-Coder/studip-api](https://github.com/N-Coder/studip-api) and [N-Coder/studip-fuse @dev2 Branch (pre version 3)](https://github.com/N-Coder/studip-fuse/tree/dev2)
- [CollapsedDom/Stud.IP-Client](https://github.com/CollapsedDom/Stud.IP-Client)
- [rockihack/Stud.IP-FileSync](https://github.com/rockihack/Stud.IP-FileSync)
- [Sync My Stud.IP (SMSIP)](https://www.flashtek.de/?view=coding.smsip)
- [woefe/studip-sync](https://github.com/woefe/studip-sync)
- [Xceron/filecrawl](https://github.com/Xceron/filecrawl)

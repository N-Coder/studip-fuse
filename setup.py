#!/usr/bin/env python3
import email
import os
import re

from setuptools import Command, Distribution, find_packages, setup


def run_cmd(*args):
    import subprocess
    try:
        return True, subprocess.check_output(args, stderr=subprocess.STDOUT).decode('ascii').strip()
    except OSError as e:
        return False, "unknown (errno %s)" % e.errno
    except subprocess.CalledProcessError as e:
        return False, "unknown (ret %s)" % e.returncode
    except Exception as e:
        return False, "unknown (%s)" % e


def install_info():
    import sys
    cmd = sys.argv
    # pip3 install --user .
    # pip3 install --user --editable .
    # python3 ./setup.py install --user
    # python3 ./setup.py develop --user

    git_rev_valid, git_rev = run_cmd("git", "describe", "--always")
    git_remote_valid, git_remote = run_cmd("git", "remote")
    if git_remote_valid:
        git_remote_url_valid, git_remote_url = run_cmd("git", "remote", "get-url", git_remote.split()[0])
    else:
        git_remote_url_valid, git_remote_url = git_remote_valid, git_remote

    return {
        'install-git-revision': git_rev,
        'install-cmd': cmd,
        'install-cwd': os.getcwd(),
        'install-repo-url': git_remote_url,
    }


class InstallNautilusPlugin(Command):
    description = "install a nautilus extension that displays file status and adds a context menu entry linking files " \
                  "to their counterpart on the studip website"
    user_options = []

    def initialize_options(self):
        pass

    def finalize_options(self):
        pass

    def run(self):
        print("Trying to load Nautilus Extension PyGObject bindings...")
        from studip_fuse.ext.nautilus_plugin import install_plugin
        install_plugin()


class MakeWindowsShortcut(Command):
    description = "create a link on the windows desktop that starts studip-fuse when clicked"
    user_options = [
        # The format is (long option, short option, description).
        ("args=", None, "arguments passed to studip-fuse when launching"),
    ]

    def initialize_options(self):
        self.args = ""

    def finalize_options(self):
        from studip_fuse.launcher.cmd_util import parse_args
        import appdirs
        import shlex
        dirs = appdirs.AppDirs(appname="Stud.IP-Fuse", appauthor=False)
        parse_args(dirs, shlex.split(self.args), prog="setup.py make_windows_shortcut --args=")

    def run(self):
        dist = self.distribution  # type: Distribution
        dist.fetch_build_eggs([
            "pywin32"
        ])
        from studip_fuse.ext.windows_shortcut import make_shortcut
        make_shortcut(self.args)


if __name__ == "__main__":
    os.chdir(os.path.dirname(os.path.realpath(__file__)))

    with open("studip_fuse/__init__.py", "r") as file:
        contents = file.read()
        version = (  # distutils.version.LooseVersion(
            re.search('^__version__\s*=\s*"(.*)"', contents, re.M).group(1))
        author, author_email = email.utils.parseaddr(
            re.search('^__author__\s*=\s*"(.*)"', contents, re.M).group(1))

    with open("README.md", "rb") as f:
        long_descr = f.read().decode("utf-8")

    setup(
        name="studip-fuse",
        packages=find_packages(),
        package_data={
            'studip_fuse': ['launcher/logging.yaml'],
        },
        install_requires=[
            "refuse==0.0.3",

            # Launcher Requirements
            "appdirs",
            "pyyaml",
            "oauthlib",
            "setuptools-meta",

            # AsyncIO Requirements
            "aiohttp",
            "aiofiles",
            "async-lru",

            # Utils
            "attrs",
            "cached_property",
            "more_itertools",
            "pyrsistent",
            "tabulate",
            "beautifulsoup4",
            "lxml",
            "yarl",

            # Compatibility
            "typing_extensions",  # for AsyncGenerator/AsyncContextManager pre 3.6
            "async-generator",  # for @async_generator pre 3.6 and @asynccontextmanager pre 3.7
            "async-exit-stack",  # for AsyncExitStack pre 3.7
            "pep487",  # for __init_subclass__ pre 3.6
        ],
        entry_points={
            "console_scripts": [
                "studip-fuse = studip_fuse.launcher.main:main"
            ]
        },
        cmdclass={
            "install_nautilus_plugin": InstallNautilusPlugin,
            "make_windows_shortcut": MakeWindowsShortcut
        },
        setup_requires=[
            "setuptools-meta"
        ],
        dependency_links=[
            "git+https://github.com/noirbizarre/setuptools-meta.git#egg=setuptools-meta"
        ],
        classifiers=[
            "License :: OSI Approved :: GNU General Public License v3 (GPLv3)",
            "Development Status :: 5 - Production/Stable",
            "Framework :: AsyncIO",
            "Programming Language :: Python :: 3.5",
            "Programming Language :: Python :: 3.6",
            "Programming Language :: Python :: 3.7",
            "Programming Language :: Python :: 3.8",
            "Topic :: System :: Filesystems",
        ],
        python_requires='>=3.5.3',  # https://github.com/aio-libs/aiohttp/blob/master/docs/faq.rst#why-is-python-3-5-3-the-lowest-supported-version
        version=version,
        meta=install_info(),
        description="Python FUSE drive for courses and files available through the Stud.IP University Access Portal",
        long_description=long_descr,
        long_description_content_type='text/markdown',
        author=author,
        author_email=author_email,
        license='GPLv3',
        url="https://github.com/N-Coder/studip-fuse"
    )

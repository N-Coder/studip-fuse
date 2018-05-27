#!/usr/bin/env python3

import os
import re

from setuptools import find_packages, setup

if __name__ == "__main__":
    os.chdir(os.path.dirname(os.path.realpath(__file__)))

    with open("studip_fuse/__init__.py", "r") as file:
        version = re.search('^__version__\s*=\s*"(.*)"', file.read(), re.M).group(1)
        author = re.search('^__author__\s*=\s*"(.*)"', file.read(), re.M).group(1)

    with open("README.md", "rb") as f:
        long_descr = f.read().decode("utf-8")

    setup(
        name="studip-fuse",
        packages=find_packages(),
        package_data={
            'studip_fuse': ['launcher/logging.yaml'],
        },
        install_requires=[
            "fusepy",

            # Launcher Requirements
            "argparse",
            "appdirs",
            "pyyaml",

            # AsyncIO Requirements
            "asyncio",
            "async-timeout",
            "async_generator",
            "aiofiles",
            "aiohttp",

            # Failsafe / Caching Requirements
            "pyfailsafe",
            "aiocache",

            # Utils
            "attrs",
            "cattrs",
            "cached_property",
            "frozendict",
            "more_itertools",
            "tabulate",
        ],
        entry_points={
            "console_scripts": [
                "studip-fuse = studip_fuse.launcher:main",
                "studip-fuse-install-nautilus-plugin = studip_fuse.ext.nautilus_plugin:main",
            ]
        },
        version=version,
        description="Python FUSE drive for courses and files available through the Stud.IP University Access Portal",
        long_description=long_descr,
        author=author,
        url="https://github.com/N-Coder/studip-fuse"
    )

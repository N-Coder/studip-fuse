#!/usr/bin/env python3

import os
import re

from setuptools import setup

if __name__ == "__main__":
    os.chdir(os.path.dirname(os.path.realpath(__file__)))

    with open("studip_fuse/__init__.py", "r") as file:
        version = re.search('^__version__\s*=\s*"(.*)"', file.read(), re.M).group(1)

    with open("README.md", "rb") as f:
        long_descr = f.read().decode("utf-8")

    setup(
        name="studip-fuse",
        packages=["studip_fuse"],
        include_package_data=True,
        install_requires=[
            "studip-api=="+version,
            "fusepy",
            "argparse",
            "appdirs"
        ],
        entry_points={
            "console_scripts": ["studip-fuse = studip_fuse.__main__:main"]
        },
        version=version,
        description="Python FUSE drive for courses and files available through the Stud.IP University Access Portal",
        long_description=long_descr,
        author="Simon Fink",
        url="https://github.com/N-Coder/studip-fuse"
    )

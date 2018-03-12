#!/usr/bin/env python3

import os
import re

from setuptools import find_packages, setup

if __name__ == "__main__":
    os.chdir(os.path.dirname(os.path.realpath(__file__)))

    with open("studip_fuse/__init__.py", "r") as file:
        version = re.search('^__version__\s*=\s*"(.*)"', file.read(), re.M).group(1)

    with open("README.md", "rb") as f:
        long_descr = f.read().decode("utf-8")

    setup(
        name="studip-fuse",
        packages=find_packages(),
        package_data={
            'studip_fuse': ['__main__/logging.yaml'],
        },
        install_requires=[
            "studip-api",  # ==" + version, # Version in dependency_links not detected correctly
            "fusepy",
            "argparse",
            "appdirs",
            # "sh",
            "pyyaml",
            "frozendict",
            "tabulate",
            "flask"
        ],
        dependency_links=[
            "git+https://github.com/N-Coder/studip-api#egg=studip-api"  # -" + version
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

import argparse as argparse
import functools
import os

import attr

from studip_api.session import StudIPSession
from studip_fuse.async_cache import schedule_task


@attr.s(hash=False)
class CachedStudIPSession(StudIPSession):
    @functools.lru_cache()
    @schedule_task()
    async def get_semesters(self):
        return await super().get_semesters()

    @functools.lru_cache()
    @schedule_task()
    async def get_courses(self, semester):
        return await super().get_courses(semester)

    @functools.lru_cache()
    @schedule_task()
    async def get_course_files(self, course):
        return await super().get_course_files(course)

    @functools.lru_cache()
    @schedule_task()
    async def get_folder_files(self, folder):
        return await super().get_folder_files(folder)


async def main():
    with open(os.path.expanduser('~/.studip-pwd')) as f:
        password = f.read()

    parser = argparse.ArgumentParser(description='Stud.IP Fuse')
    parser.add_argument('user', help='username')
    args = parser.parse_args()

    session = await CachedStudIPSession(
        user_name=args.user,
        password=password,
        studip_base="https://studip.uni-passau.de",
        sso_base="https://sso.uni-passau.de"
    ).__aenter__()
    password = ""

    # TODO remove
    session.get_semesters().add_done_callback(
        lambda sf: [session.get_courses(semester).add_done_callback(
            lambda cf: [session.get_course_files(course).add_done_callback(
                lambda cff: [session.get_folder_files(folder) for folder in cff.result().contents]
            ) for course in cf.result()]
        ) for semester in sf.result() if semester.name in ["WS 17/18"]])

    return session

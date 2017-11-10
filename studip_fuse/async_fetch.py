import asyncio
import os
from asyncio.futures import _chain_future as chain_future

import argparse as argparse
import attr

from studip_api.session import StudIPSession


@attr.s
class AsyncState(object):  # TODO replace by concurrent.futures and load lazily / only when awaited
    root: asyncio.Future = attr.ib(default=attr.Factory(asyncio.Future))
    semesters: asyncio.Future = attr.ib(default=attr.Factory(asyncio.Future))  # [List[Semester]]
    courses: asyncio.Future = attr.ib(default=attr.Factory(asyncio.Future))  # [List[Course]]
    files: asyncio.Future = attr.ib(default=attr.Factory(asyncio.Future))  # [List[Future[File]]]


async def main(state):
    with open(os.path.expanduser('~/.studip-pwd')) as f:
        password = f.read()

    parser = argparse.ArgumentParser(description='Stud.IP Fuse')
    parser.add_argument('user',                        help='username')
    args = parser.parse_args()

    async with StudIPSession(
            user_name=args.user,
            password=password,
            studip_base="https://studip.uni-passau.de",
            sso_base="https://sso.uni-passau.de"
    ) as session:
        password = ""

        # Track all pending file listings for courses and recurse into subfolders
        all_files_done, handle_files_future_started = track_pending_files(session)

        # List semesters and their courses
        semester_future = await load_semesters(session, state)
        courses_futures = await load_courses(session, state, semester_future, handle_files_future_started)

        # Wait for all listings to complete
        await semester_future
        for course_future in courses_futures:
            await course_future
        all_file_futures = await all_files_done()
        chain_future(asyncio.gather(*all_file_futures, return_exceptions=True), state.files)


def track_pending_files(session):
    all_file_futures = []
    files_done_condition = asyncio.Condition()

    # This coroutine returns once all file listings are loaded
    async def all_files_done():
        async with files_done_condition:
            await files_done_condition.wait_for(lambda: all(f.done() for f in all_file_futures))
            return all_file_futures

    # When a listing is complete, also recurse into its subfolders and notify the Condition
    async def handle_files_future_done(get_files_future):
        has_subfolders = False
        for file in get_files_future.result().contents:
            if file.is_folder():
                has_subfolders = True
                handle_files_future_started(asyncio.ensure_future(session.get_folder_files(file)))
        # No need to notify the files_done_condition condition if we just requested a listing for subfolders
        if not has_subfolders:
            async with files_done_condition:
                files_done_condition.notify_all()

    # Add a pending listing to the list of all futures and track its return value
    def handle_files_future_started(get_files_future):
        get_files_future.add_done_callback(lambda f: asyncio.ensure_future(handle_files_future_done(f)))
        all_file_futures.append(get_files_future)

    return all_files_done, handle_files_future_started


async def load_semesters(session, state):
    semester_future = asyncio.ensure_future(session.get_semesters())
    chain_future(semester_future, state.semesters)
    return semester_future


async def load_courses(session, state, semester_future, handle_files_future):
    courses_futures = []
    for s in await semester_future:
        if s.name not in ["WS 17/18"]:
            continue
        f = asyncio.ensure_future(session.get_courses(s))
        courses_futures.append(f)
        for course in await f:
            # if course.number not in ["5792UE", "5792V", "5792"]:
            #     continue
            handle_files_future(asyncio.ensure_future(session.get_course_files(course)))
    chain_future(asyncio.gather(*courses_futures), state.courses)
    return courses_futures

    # courses_futures = [asyncio.ensure_future(session.get_courses(s)) for s in await semester_future]
    # for course_future in courses_futures:
    #     course_future.add_done_callback(lambda cf: (
    #         handle_files_future(session.get_course_files(course))
    #         for course in cf.result()))

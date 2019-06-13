import datetime
import os
from csv import DictReader
from io import FileIO, TextIOWrapper
from typing import Optional
from zipfile import ZipFile, ZipInfo

import attr

from studip_fuse.studipfs.api.aiobase import BaseDownload
from studip_fuse.studipfs.api.aiointerface import Download, StudIPSession


class StudIPCompatSession(StudIPSession):
    async def check_login(self, username):
        pass

    async def prefetch_globals(self):
        pass

    async def get_instance_name(self):
        pass

    async def get_user(self):
        pass

    def get_semesters(self):
        pass

    def get_courses(self, semester):
        pass

    async def download_course_zip(self, courseid):
        zip_download = await self.http.retrieve(
            uid=courseid,
            url=self.studip_url("bulk/%s" % courseid),
        )
        await zip_download.start_loading()
        return zip_download

    def extract_archive_filelist(self, zip_download):
        zip = ZipFile(zip_download.open_sync())
        return list(DictReader(TextIOWrapper(zip.open("archive_filelist.csv"))))

    async def get_course_root_folder(self, course):
        pass

    async def get_folder_details(self, parent):
        pass

    async def get_file_details(self, parent):
        pass

    async def retrieve_file(self, file):
        zip_download = await self.download_course_zip(file["courseid"])

        zip = ZipFile(zip_download.open_sync())
        info = zip.getinfo(file["path"])
        size = int(file["size"])
        assert size == info.file_size
        return ZipContentDownload(
            uid=file["id"],
            url=self.studip_url("file/%s/download" % file["id"]),
            last_modified=datetime.fromtimestamp(int(file["chdate"])),
            total_length=size,
            local_path=zip_download.local_path + "#" + info.filename,
            zip_download=zip_download,
            content_info=info
        )


@attr.s()
class ZipContentDownload(BaseDownload):
    zip_download = attr.ib()  # type: Download
    content_info = attr.ib()  # type: Optional[ZipInfo]

    @property
    def zip_content_range(self):
        from zipfile import sizeFileHeader
        # FIXME missing filename, extra and crypto prefix length
        # the exact size could be calculated from
        # ZipExtFile._orig_compress_start = fileobj.tell()
        # ZipExtFile._orig_compress_size = zipinfo.compress_size
        return self.content_info.header_offset, self.content_info.compress_size + sizeFileHeader

    @property
    def is_loading(self) -> bool:
        return self.zip_download.is_loading and not self.is_completed

    @property
    def is_completed(self) -> bool:
        return self.zip_download.is_completed or self.zip_download.is_readable(*self.zip_content_range)

    @property
    def exception(self) -> BaseException:
        return self.zip_download.exception

    async def start_loading(self, offset=0, length=-1):
        return await self.zip_download.start_loading(*self.zip_content_range)

    def readable_bytes(self, offset=0) -> int:
        if self.zip_download.is_readable(*self.zip_content_range):
            return self.content_info.file_size
        else:
            return 0

    async def await_readable(self, offset=0, length=-1, start_loading=False):
        return await self.zip_download.await_readable(*self.zip_content_range, start_loading=start_loading)

    async def open_sync(self, flags=0) -> FileIO:
        zip = ZipFile(self.zip_download.open_sync(flags))
        return zip.open(self.content_info, "w" if flags & os.O_WRONLY else "r")

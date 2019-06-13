
import re
import urllib.parse as urlparse
import warnings
from datetime import datetime
from typing import Optional

import attr
from bs4 import BeautifulSoup

DUPLICATE_TYPE_RE = re.compile(r'^(?P<type>(Plenarü|Tutorü|Ü)bung(en)?|Tutorium|Praktikum'
                               + r'|(Obers|Haupts|S)eminar|Lectures?|Exercises?)(\s+(f[oü]r|on|zu[rm]?|i[nm]|auf))?'
                               + r'\s+(?P<name>.+)')
COURSE_NAME_TYPE_RE = re.compile(r'(.*?)\s*\(\s*([^)]+)\s*\)\s*$')

DATE_FORMATS = ['%d.%m.%Y %H:%M:%S', '%d/%m/%y %H:%M:%S']


def compact(str):
    return " ".join(str.split())


def get_url_field(url, field):
    parsed_url = urlparse.urlparse(url)
    query = urlparse.parse_qs(parsed_url.query, encoding="iso-8859-1")
    return query[field][0] if field in query else None


def get_file_id_from_url(url):
    return re.findall("/studip/dispatch\.php/(course/files/index|file/details)/([a-z0-9]+)\?", url)[0][1]


def parse_date(date: str):
    exc = None
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(date, fmt)
        except ValueError as e:
            exc = e
    raise ParserError('Invalid date format') from exc


class URL(object):
    @staticmethod
    def login_page():
        return "https://studip.uni-passau.de/studip/index.php?again=yes&sso=shib"

    @staticmethod
    def files_main():
        return "https://studip.uni-passau.de/studip/dispatch.php/course/files"

    @staticmethod
    def bulk_download(folder_id):
        return "https://studip.uni-passau.de/studip/dispatch.php/file/bulk/{}".format(folder_id)

    @staticmethod
    def studip_main():
        return "https://studip.uni-passau.de/Shibboleth.sso/SAML2/POST"

    @staticmethod
    def courses():
        return "https://studip.uni-passau.de/studip/dispatch.php/my_courses"

def get_couses(self):
    with self.session.get(URL.courses()) as response:
        if not response.ok:
            raise SessionError("Failed to get courses")

        return parsers.extract_courses(response.text)

def download(self, course_id, workdir, sync_only=None):
    params = {"cid": course_id}

    with self.session.get(URL.files_main(), params=params) as response:
        if not response.ok:
            raise DownloadError("Cannot access course files page")
        folder_id = parsers.extract_parent_folder_id(response.text)
        csrf_token = parsers.extract_csrf_token(response.text)

    download_url = URL.bulk_download(folder_id)
    data = {
        "security_token": csrf_token,
        # "parent_folder_id": folder_id,
        "ids[]": sync_only or folder_id,
        "download": 1
    }

    with self.session.post(download_url, params=params, data=data, stream=True) as response:
        if not response.ok:
            raise DownloadError("Cannot download course files")
        path = os.path.join(workdir, course_id)
        with open(path, "wb") as download_file:
            shutil.copyfileobj(response.raw, download_file)
    return path

def parse_user_selection(html):
    soup = BeautifulSoup(html, 'lxml')

    selected_semester = soup.find('select', {'name': 'sem_select'}).find('option', {'selected': True})
    if not selected_semester:
        # default to first if none is selected
        selected_semester = soup.find('select', {'name': 'sem_select'}).find('option')
    selected_semester = selected_semester.attrs['value']
    selected_ansicht = soup.find(
        'a', class_="active",
        href=re.compile("my_courses/store_groups\?select_group_field")
    ).attrs['href']

    return selected_semester, get_url_field(selected_ansicht, "select_group_field")


def parse_semester_list(html):
    soup = BeautifulSoup(html, 'lxml')

    for item in soup.find_all('select', {'name': 'sem_select'}):
        options = item.find('optgroup').find_all('option')
        for i, option in enumerate(options):
            yield Semester(
                id=option.attrs['value'], name=compact(option.contents[0]), order=len(options) - 1 - i
            )


def parse_course_list(html, semester: Semester):
    soup = BeautifulSoup(html, 'lxml')
    current_number = semester_str = None
    invalid_semester = found_course = False

    for item in soup.find_all('div', {'id': 'my_seminars'}):
        semester_str = item.find('caption').text.strip()
        if not semester_str == semester.name:
            invalid_semester = True
            warnings.warn(
                "Ignoring courses for %s found while searching for the courses for %s" % (semester_str, semester.name))
            continue

        for tr in item.find_all('tr'):
            if 'class' in tr.attrs:
                continue

            for td in tr.find_all('td'):
                if len(td.attrs) == 0 and len(td.find_all()) == 0 and td.text.strip():
                    current_number = td.text.strip()

                link = td.find('a')
                if not link:
                    continue
                full_name = compact(link.contents[0])
                name, course_type = COURSE_NAME_TYPE_RE.match(full_name).groups()
                match = DUPLICATE_TYPE_RE.match(name)
                if match:
                    course_type = match.group("type")
                    name = match.group("name")
                found_course = True
                yield Course(
                    id=get_url_field(link['href'], 'auswahl').strip(),
                    semester=semester,
                    number=current_number,
                    name=name, type=course_type
                )
                break

    if invalid_semester and not found_course:
        raise ParserError("Only found courses for %s while searching for the courses for %s"
                          % (semester_str, semester.name), soup)

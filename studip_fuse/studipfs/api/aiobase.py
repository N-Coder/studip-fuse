import os
from abc import abstractmethod
from datetime import datetime
from typing import Dict, Optional

import attr
from bs4 import BeautifulSoup
from yarl import URL

from studip_fuse.studipfs.api.aiointerface import Download, HTTPClient


@attr.s()
class BaseHTTPClient(HTTPClient):
    storage_dir = attr.ib()  # type: str
    download_cache = attr.ib(init=False, default=attr.Factory(dict))  # type: Dict[str, "AiohttpDownload"]

    def uid_to_path(self, uid):
        return os.path.join(self.storage_dir, uid)

    @abstractmethod
    async def retrieve_missing(self, uid, url, overwrite_created, expected_size):
        pass

    async def retrieve(self, uid: str, url: str, overwrite_created: Optional[datetime] = None, expected_size: Optional[int] = None) -> "Download":
        if uid in self.download_cache:
            download = self.download_cache[uid]
            assert download.url == URL(url)
            assert download.total_length is None or download.total_length == expected_size
            assert download.last_modified is None or download.last_modified == overwrite_created
            return download
        else:
            download = await self.retrieve_missing(uid, url, overwrite_created, expected_size)
            self.download_cache[uid] = download
            return download

    @staticmethod
    def parse_login_form(html):
        soup = BeautifulSoup(html, 'lxml')
        for form in soup.find_all('form'):
            if 'action' in form.attrs:
                return form.attrs['action']
        raise PermissionError("Could not find login form", soup)

    @staticmethod
    def parse_saml_form(html):
        soup = BeautifulSoup(html, 'lxml')
        saml_fields = {'RelayState', 'SAMLResponse'}
        form_data = {}
        form_url = None
        p = soup.find('p')
        if 'class' in p.attrs and 'form-error' in p.attrs['class']:
            raise PermissionError("Error in Request: '%s'" % p.text, soup)
        for input_elem in soup.find_all('input'):
            if 'name' in input_elem.attrs and 'value' in input_elem.attrs and input_elem.attrs['name'] in saml_fields:
                form_data[input_elem.attrs['name']] = input_elem.attrs['value']
                form_url = input_elem.find_parent("form").attrs['action']

        return form_url, form_data

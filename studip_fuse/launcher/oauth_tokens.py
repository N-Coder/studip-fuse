from typing import Dict

from yarl import URL

OAUTH_TOKENS = {
    URL('https://studip.uni-passau.de/studip/api.php/'): (
        "s480n14norqnq1s112ss4s288o0o03qq05p6os8q9",
        "8r148q1786nssrr06r75n5nq96sq124s")
}  # type: Dict[URL, (str, str)] # maps URL -> (client_key, client_secret)

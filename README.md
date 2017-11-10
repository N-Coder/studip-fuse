Stud.IP FUSE driver
===================

**TODO** rewrite

_studip-client_ is currently only implemented for the University of Passau (https://uni-passau.de/).
Make sure you have at least Python 3.4 installed.


_studip-client_ works by crawling the Stud.IP web interface and will therefore ask for your
username and password. The credentials are stored locally in `<sync-dir>/.studip/studip.conf` and
encrypted with a machine-local auto-generated key found in `~/.cache/studip/secret` so that
simply obtaining a copy of your config file is not enough to recover your password.

All connections to the university servers transporting the login data are made via HTTPS.
Your credentials will not be copied or distributed in any other way.

If you're interested in verifying this claim manually, the relevant source code can be found in
`studip/application.py`, `Application.open_session()`.

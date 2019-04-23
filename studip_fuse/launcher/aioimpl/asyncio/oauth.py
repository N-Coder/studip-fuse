import asyncio
import logging
from urllib.parse import parse_qsl, urlencode

import aiohttp
from aiohttp import ClientError
from aiohttp.web import AppRunner, Application, HTTPBadGateway, HTTPBadRequest, HTTPClientError, HTTPException, HTTPFound, HTTPUnauthorized, Response, RouteTableDef, TCPSite
from oauthlib.common import generate_nonce
from oauthlib.oauth1 import Client as OAuth1Client
from yarl import URL

from studip_fuse.launcher.cmd_util import get_environment, get_version
from studip_fuse.studipfs.api.session import StudIPAPISession
from studip_fuse.studipfs.api.aiointerface import StudIPSession

OAUTH_CALLBACK_PORT = 17548

HEADER = """<html><head><meta charset="UTF-8"></head>
<body style="font-family: sans-serif">
<table style="width: 800px; margin-left: auto; margin-right: auto;"><tbody><tr>"""
FOOTER = """</td></tr></tbody></table></body></html>"""

__all__ = ["obtain_access_token", "obtain_access_token_sessionless", "get_tokens"]

log = logging.getLogger(__name__)


def url_for(name, request=None, site=None, base_url=None, app=None, router=None):
    if base_url is None:
        if request is not None:
            base_url = request.url
        elif site is not None:
            base_url = URL(site.name)
        else:
            raise ValueError("no base_url")

    if app is None and request is not None:
        app = request.app
    if router is None:
        if app is not None:
            router = app.router
        else:
            raise ValueError("no router")

    return base_url.join(router[name].url_for())


def request_error(request, error, message, clazz=HTTPBadGateway):
    log.error(message, exc_info=error)
    return clazz(
        body="%s<tr><td style='font-weight: bold; font-size:120%%'>%s</td></tr>"
             "<tr><td style='font-style: italic'>Error: %s</td></tr>"
             "<tr><td><a href='%s'>Click here to start over!</a></td></tr>%s" %
             (HEADER, message, error, url_for("start_oauth", request), FOOTER),
        content_type="text/html")


async def make_subrequest(r, method, url, **kwargs):
    try:
        async with r.app["http_session"].request(method, url, **kwargs) as response:
            response.raise_for_status()
            if 'json' in response.headers.get('CONTENT-TYPE'):
                data = await response.json()
            else:
                data = await response.text()

                data = dict(parse_qsl(data))
            return data
    except (HTTPClientError, ClientError) as e:
        raise request_error(r, e, "Could not communicate with OAuth1 server %s" % url) from e


def update_client(oauth1_client, **kwargs):
    for key, value in kwargs.items():
        log.debug("%-25s %s", key, value)
        setattr(oauth1_client, key, value)


routes = RouteTableDef()


@routes.get("/")
async def index(request):
    return Response(text="This server was started by %s to obtain an OAuth1 token." % get_version(details=False), headers={
        "Server": get_environment()
    })


@routes.get("/check", name="check_running")
async def check_running(request):
    log.debug("check_running %s", request.query["nonce"])
    request.app["check_running_future"].set_result(request.query["nonce"])
    return Response(text="running")


@routes.get("/request", name="start_oauth")
async def oauth_request(request):
    oauth1_client = request.app["oauth1_client"]  # type: OAuth1Client
    studip_session = request.app["studip_session"]  # type: StudIPSession

    update_client(
        oauth1_client,
        resource_owner_key=None,
        resource_owner_secret=None,
        verifier=None
    )
    data = await make_subrequest(request, 'GET', studip_session.oauth1_urls.request_token)
    update_client(
        oauth1_client,
        resource_owner_key=data.get('oauth_token'),
        resource_owner_secret=data.get('oauth_token_secret'),
    )

    # authorize_url = URL(studip_session.oauth1_urls.authorize).with_query(dict(
    authorize_url = str(studip_session.oauth1_urls.authorize) + '?' + urlencode(dict(
        oauth_callback=str(url_for("oauth_callback", request)),  # XXX must be escaped, otherwise website won't show name
        oauth_token=oauth1_client.resource_owner_key
    ))
    raise HTTPFound(authorize_url)


@routes.get("/callback", name="oauth_callback")
async def oauth_callback(request):
    oauth1_client = request.app["oauth1_client"]  # type: OAuth1Client
    studip_session = request.app["studip_session"]  # type: StudIPSession

    if not all(k in request.query for k in ["oauth_verifier", "oauth_token"]) \
            or oauth1_client.resource_owner_key != request.query["oauth_token"]:
        raise request_error(request, None, "OAuth Access was denied.")

    update_client(
        oauth1_client,
        verifier=request.query["oauth_verifier"],
        resource_owner_key=request.query["oauth_token"],
    )

    data = await make_subrequest(request, 'POST', studip_session.oauth1_urls.access_token)
    update_client(
        oauth1_client,
        resource_owner_key=data.get('oauth_token'),
        resource_owner_secret=data.get('oauth_token_secret'),
    )

    try:
        user = await studip_session.check_login()
    except (HTTPClientError, ClientError) as e:
        raise request_error(request, e, "Login failed", HTTPBadRequest) from e

    request.app["finished_client_future"].set_result(oauth1_client)
    return Response(
        body="""%s
<td style="vertical-align: top; padding-right: 10px"><img src="%s"></td><td>
<p>Successfully logged in as %s, welcome %s!<br>You may close this window, the FUSE driver will finish starting up in the background.</p>
<p style="font-size:70%%">%s. <a href="https://github.com/N-Coder/StudIP-FUSE">Source</a></p>
<p style="font-size:70%%">%s. <a href="%s">Details</a></p>
%s""" %
             (HEADER,
              user["avatar_normal"], user["name"]["username"], user["name"]["formatted"],
              get_environment(),
              await studip_session.get_instance_name(), studip_session.studip_url("/studip/dispatch.php/siteinfo/"),
              FOOTER),
        content_type="text/html"
    )


def get_tokens(url):
    import codecs
    from studip_fuse.launcher.oauth_tokens import OAUTH_TOKENS
    url = URL(url)
    if url not in OAUTH_TOKENS:
        raise ValueError("Unknown URL %s, please contact the site's admin to get a token" % url)
    # making it at least a little harder to steal the keys
    return tuple(codecs.encode(s, "rot13") for s in OAUTH_TOKENS[url])


async def obtain_access_token(studip_session: StudIPSession, oauth1_client: OAuth1Client = None,
                              http_session: aiohttp.ClientSession = None, port=OAUTH_CALLBACK_PORT, open_browser=True):
    if not http_session:
        http_session = studip_session.http.http_session
    if not oauth1_client:
        if isinstance(http_session._default_auth, OAuth1Client):
            oauth1_client = http_session._default_auth
        else:
            oauth1_client = OAuth1Client(*get_tokens(studip_session.studip_base))
            http_session._default_auth = oauth1_client

    try:
        await studip_session.check_login()
        return oauth1_client
    except (HTTPException, aiohttp.ClientResponseError) as e:
        if e.status != HTTPUnauthorized.status_code \
                and getattr(e, "message", "") != "Can't verify request, missing oauth_consumer_key or oauth_token":
            raise
        log.info("OAuth Session invalid, starting log-in flow")

    app = Application()
    app.add_routes(routes)

    app["http_session"] = http_session
    app["studip_session"] = studip_session
    app["oauth1_client"] = oauth1_client
    app["finished_client_future"] = asyncio.get_event_loop().create_future()
    app["check_running_future"] = asyncio.get_event_loop().create_future()

    runner = AppRunner(app)
    try:
        await runner.setup()
        site = TCPSite(runner, host="127.0.0.1", port=port)
        await site.start()

        nonce = generate_nonce()
        log.debug("nonce         %s", nonce)
        async with http_session.get(url_for("check_running", app=app, site=site).with_query(nonce=nonce))as resp:
            resp.raise_for_status()
            assert await resp.text() == "running"
            assert await asyncio.wait_for(app["check_running_future"], 0) == nonce
        log.debug("server check passed")

        start_addr = str(url_for("start_oauth", app=app, site=site))
        log.info("Go to the following address to log in via OAuth1: %s", start_addr)
        if open_browser is True:
            import webbrowser
            open_browser = webbrowser.open
        if callable(open_browser):
            await asyncio.get_event_loop().run_in_executor(None, lambda: open_browser(start_addr)),
        return await app["finished_client_future"]
    finally:
        await runner.cleanup()


async def obtain_access_token_sessionless(oauth1_client: OAuth1Client = None,
                                          studip_url="https://studip.uni-passau.de/studip/api.php/",
                                          port=OAUTH_CALLBACK_PORT, open_browser=True):
    from studip_fuse.launcher.aioimpl.asyncio import AuthenticatedClientRequest, HTTPClient

    client_session = aiohttp.ClientSession(
        headers={"User-Agent": get_environment()},
        request_class=AuthenticatedClientRequest,
        auth=oauth1_client
    )  # will be aentered/aexited by http_client
    async with HTTPClient(http_session=client_session, storage_dir=None) as http_client:
        studip_session = StudIPAPISession(studip_base=studip_url, http=http_client)
        return await obtain_access_token(studip_session, oauth1_client, port=port, open_browser=open_browser)


if __name__ == "__main__":
    from logging import DEBUG

    logging.basicConfig(level=DEBUG)
    print(asyncio.run(obtain_access_token_sessionless()))

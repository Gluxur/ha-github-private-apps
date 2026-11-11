"""Read-only git smart-HTTP relay.

The Supervisor clones and pulls app repositories through this relay using a
URL that never contains a credential, e.g.
``http://172.30.32.1:48123/owner/repo.git``. The relay forwards the two
requests git needs for a fetch to GitHub and adds the current credential.

This module has no Home Assistant dependency so it can be tested on its own.
"""

from __future__ import annotations

import base64
from collections.abc import Callable
import hmac
from ipaddress import IPv4Network, IPv6Network, ip_address
import logging
import re

from aiohttp import ClientError, ClientSession, ClientTimeout, web

_LOGGER = logging.getLogger(__name__)

# /<secret>/<owner>/<repo>.git/info/refs  and  .../git-upload-pack
# The secret is fixed for the life of the installation (it is part of the
# URL the Supervisor stores, hence of the slug). It keeps other apps on the
# Supervisor network from reading private repositories through the relay.
_PATH = re.compile(
    r"^/(?P<secret>[A-Za-z0-9_-]{16,128})/"
    r"(?P<owner>[A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))/"
    r"(?P<repo>[A-Za-z0-9._-]{1,100}?)(?:\.git)?/"
    r"(?P<endpoint>info/refs|git-upload-pack)$"
)

_FORWARD_REQUEST_HEADERS = ("Content-Type", "Content-Encoding", "Accept", "Git-Protocol")
_FORWARD_RESPONSE_HEADERS = ("Content-Type", "Content-Encoding", "Cache-Control", "Expires", "Pragma")

# Returns (login, token) for "owner/repo", or None if the repo is not served.
CredentialLookup = Callable[[str], "tuple[str, str] | None"]
AuthFailureCallback = Callable[[str, int], None]


def basic_auth(login: str, token: str) -> str:
    """Build a Basic Authorization header value."""
    raw = f"{login}:{token}".encode()
    return "Basic " + base64.b64encode(raw).decode()


class GitRelay:
    """aiohttp application relaying git-upload-pack to GitHub."""

    def __init__(
        self,
        session: ClientSession,
        secret: str,
        credentials: CredentialLookup,
        allowed_networks: list[IPv4Network | IPv6Network],
        upstream: str = "https://github.com",
        on_auth_failure: AuthFailureCallback | None = None,
    ) -> None:
        self._session = session
        self._secret = secret
        self._credentials = credentials
        self._allowed = allowed_networks
        self._upstream = upstream.rstrip("/")
        self._on_auth_failure = on_auth_failure
        self._runner: web.AppRunner | None = None

    def build_app(self) -> web.Application:
        """Create the aiohttp application."""
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", self._handle)
        return app

    async def start(self, host: str, port: int) -> None:
        """Start listening."""
        self._runner = web.AppRunner(self.build_app(), access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, host, port, reuse_address=True)
        await site.start()
        _LOGGER.info("Git relay listening on http://%s:%s", host, port)

    async def stop(self) -> None:
        """Stop listening."""
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    def _peer_allowed(self, request: web.Request) -> bool:
        peer = request.remote
        if not peer:
            return False
        try:
            addr = ip_address(peer)
        except ValueError:
            return False
        return any(addr in net for net in self._allowed)

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        if not self._peer_allowed(request):
            # Debug only: any app on the network could otherwise flood the log.
            _LOGGER.debug("Git relay: rejected request from %s", request.remote)
            return web.Response(status=403, text="Forbidden")

        match = _PATH.match(request.path)
        if match is None or not hmac.compare_digest(match["secret"], self._secret):
            return web.Response(status=404, text="Not found")

        endpoint = match["endpoint"]
        if endpoint == "info/refs":
            if request.method != "GET" or request.query.get("service") != "git-upload-pack":
                # Only fetch is relayed; push (git-receive-pack) is refused.
                return web.Response(status=403, text="Read-only relay")
        elif request.method != "POST":
            return web.Response(status=405, text="Method not allowed")

        full_name = f"{match['owner']}/{match['repo']}"
        creds = self._credentials(full_name.lower())
        if creds is None:
            return web.Response(status=404, text="Not found")
        login, token = creds

        url = f"{self._upstream}/{full_name}.git/{endpoint}"
        headers = {h: request.headers[h] for h in _FORWARD_REQUEST_HEADERS if h in request.headers}
        headers["Authorization"] = basic_auth(login, token)
        headers["User-Agent"] = request.headers.get("User-Agent", "git/2 github-private-apps-relay")

        data = request.content if request.method == "POST" else None
        try:
            upstream = await self._session.request(
                request.method,
                url,
                params=request.query,
                headers=headers,
                data=data,
                allow_redirects=False,
                auto_decompress=False,
                timeout=ClientTimeout(total=None, sock_connect=15, sock_read=120),
            )
        except Exception as err:  # noqa: BLE001 - surface as gateway error
            _LOGGER.warning("Git relay: upstream error for %s: %s", full_name, type(err).__name__)
            return web.Response(status=502, text="Upstream error")

        async with upstream:
            if upstream.status in (401, 403, 404) and self._on_auth_failure is not None:
                # GitHub answers 404 for private repos the token cannot see.
                self._on_auth_failure(full_name, upstream.status)

            response = web.StreamResponse(status=upstream.status)
            for name in _FORWARD_RESPONSE_HEADERS:
                if name in upstream.headers:
                    response.headers[name] = upstream.headers[name]
            await response.prepare(request)
            try:
                async for chunk in upstream.content.iter_chunked(64 * 1024):
                    await _write(response, chunk)
                await _write(response, None)
            except _ClientGone as err:
                # The Supervisor went away (its own timeout, a restart):
                # not an upstream problem.
                _LOGGER.debug(
                    "Git relay: client stopped reading %s: %s", full_name, type(err.__cause__).__name__
                )
                return _abort(request, response)
            except (ClientError, TimeoutError) as err:
                # Headers are already sent: dropping the connection is the
                # only way left to tell git the transfer failed.
                _LOGGER.warning(
                    "Git relay: transfer of %s interrupted: %s", full_name, type(err).__name__
                )
                return _abort(request, response)
            return response


class _ClientGone(Exception):
    """Writing to the downstream (Supervisor) connection failed."""


async def _write(response: web.StreamResponse, chunk: bytes | None) -> None:
    """Write a chunk, or the end of the body when chunk is None.

    Write errors are re-raised as _ClientGone so they are not mistaken for
    upstream errors (aiohttp's ClientConnectionResetError is a ClientError).
    """
    try:
        if chunk is None:
            await response.write_eof()
        else:
            await response.write(chunk)
    except ConnectionError as err:
        raise _ClientGone from err


def _abort(request: web.Request, response: web.StreamResponse) -> web.StreamResponse:
    """Drop the connection of a response whose headers are already sent.

    aiohttp's own end-of-body write on the closed transport afterwards is
    treated as a client disconnection and only logged at debug level.
    """
    response.force_close()
    if request.transport is not None:
        request.transport.close()
    return response

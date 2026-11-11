"""Tests for the git relay (no Home Assistant needed)."""

from __future__ import annotations

import asyncio
import base64
import logging
from ipaddress import ip_network
import importlib.util
from pathlib import Path

from aiohttp import ClientPayloadError, ClientSession, web
from aiohttp.test_utils import TestServer
import pytest

_spec = importlib.util.spec_from_file_location(
    "relay", Path(__file__).parents[1] / "custom_components" / "github_private_apps" / "relay.py"
)
relay_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(relay_mod)
GitRelay = relay_mod.GitRelay

SECRET = "s3cretS3cretS3cret"
REPO = "Owner/My-Repo"


@pytest.fixture
async def upstream():
    """Fake GitHub git endpoint recording what it receives."""
    seen: list[dict] = []
    status = {"code": 200}

    async def refs(request: web.Request) -> web.Response:
        seen.append({"path": request.path, "query": dict(request.query), "headers": dict(request.headers)})
        return web.Response(
            status=status["code"],
            body=b"001e# service=git-upload-pack\n0000",
            content_type="application/x-git-upload-pack-advertisement",
        )

    async def pack(request: web.Request) -> web.StreamResponse:
        body = await request.read()
        seen.append({"path": request.path, "body": body, "headers": dict(request.headers)})
        if status.get("slow"):
            # Long answer, so the relay is still writing when the client leaves.
            resp = web.StreamResponse(status=200)
            await resp.prepare(request)
            try:
                for _ in range(200):
                    await resp.write(b"x" * 65536)
                    await asyncio.sleep(0.01)
            except ConnectionError:
                pass
            finally:
                status["slow_done"].set()
            return resp
        if status.get("truncate"):
            # Start a chunked answer, then drop the connection mid-stream.
            resp = web.StreamResponse(status=200)
            resp.enable_chunked_encoding()
            await resp.prepare(request)
            await resp.write(b"PACK partial")
            request.transport.close()
            return resp
        return web.Response(status=status["code"], body=b"PACK" + body, content_type="application/x-git-upload-pack-result")

    app = web.Application()
    app.router.add_get("/{owner}/{repo}/info/refs", refs)
    app.router.add_post("/{owner}/{repo}/git-upload-pack", pack)
    server = TestServer(app)
    await server.start_server()
    yield server, seen, status
    await server.close()


@pytest.fixture
async def relay(upstream, unused_tcp_port):
    server, _, _ = upstream
    failures: list[tuple[str, int]] = []
    session = ClientSession()
    r = GitRelay(
        session,
        SECRET,
        lambda name: ("login", "tok") if name == REPO.lower() else None,
        [ip_network("127.0.0.0/8")],
        upstream=str(server.make_url("")),
        on_auth_failure=lambda n, c: failures.append((n, c)),
    )
    await r.start("127.0.0.1", unused_tcp_port)
    yield f"http://127.0.0.1:{unused_tcp_port}", failures, session
    await r.stop()
    await session.close()


async def test_info_refs_adds_auth_and_forwards(relay, upstream):
    base, _, session = relay
    _, seen, _ = upstream
    async with session.get(
        f"{base}/{SECRET}/{REPO}.git/info/refs",
        params={"service": "git-upload-pack"},
        headers={"Git-Protocol": "version=2"},
    ) as resp:
        assert resp.status == 200
        assert resp.headers["Content-Type"] == "application/x-git-upload-pack-advertisement"
        assert b"git-upload-pack" in await resp.read()
    sent = seen[-1]
    assert sent["path"] == f"/{REPO}.git/info/refs"
    assert sent["query"] == {"service": "git-upload-pack"}
    assert sent["headers"]["Authorization"] == "Basic " + base64.b64encode(b"login:tok").decode()
    assert sent["headers"]["Git-Protocol"] == "version=2"


async def test_upload_pack_streams_body(relay, upstream):
    base, _, session = relay
    _, seen, _ = upstream
    async with session.post(
        f"{base}/{SECRET}/{REPO}.git/git-upload-pack",
        data=b"0032want abc\n0000",
        headers={"Content-Type": "application/x-git-upload-pack-request"},
    ) as resp:
        assert resp.status == 200
        assert await resp.read() == b"PACK0032want abc\n0000"
    assert seen[-1]["body"] == b"0032want abc\n0000"


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (f"/wrong-secret-0000000/{REPO}.git/info/refs?service=git-upload-pack", 404),
        (f"/{SECRET}/Owner/other.git/info/refs?service=git-upload-pack", 404),
        (f"/{SECRET}/{REPO}.git/info/refs?service=git-receive-pack", 403),
        (f"/{SECRET}/{REPO}.git/info/refs", 403),
        (f"/{SECRET}/{REPO}.git/HEAD", 404),
    ],
)
async def test_rejections(relay, upstream, path, expected):
    base, _, session = relay
    _, seen, _ = upstream
    async with session.get(base + path) as resp:
        assert resp.status == expected
    assert seen == []


async def test_get_on_upload_pack_rejected(relay):
    base, _, session = relay
    async with session.get(f"{base}/{SECRET}/{REPO}.git/git-upload-pack") as resp:
        assert resp.status == 405


async def test_peer_outside_network_rejected(upstream, unused_tcp_port):
    server, seen, _ = upstream
    async with ClientSession() as session:
        r = GitRelay(
            session,
            SECRET,
            lambda name: ("login", "tok"),
            [ip_network("172.30.32.0/23")],
            upstream=str(server.make_url("")),
        )
        await r.start("127.0.0.1", unused_tcp_port)
        try:
            async with session.get(
                f"http://127.0.0.1:{unused_tcp_port}/{SECRET}/{REPO}.git/info/refs",
                params={"service": "git-upload-pack"},
            ) as resp:
                assert resp.status == 403
        finally:
            await r.stop()
    assert seen == []


async def test_upstream_401_reports_auth_failure(relay, upstream):
    base, failures, session = relay
    _, _, status = upstream
    status["code"] = 401
    async with session.get(
        f"{base}/{SECRET}/{REPO}.git/info/refs", params={"service": "git-upload-pack"}
    ) as resp:
        assert resp.status == 401
    assert failures == [(REPO, 401)]


async def test_upstream_interrupted_mid_stream(relay, upstream, caplog):
    """A broken upstream transfer fails the git request, not the relay."""
    base, _, session = relay
    _, _, status = upstream
    status["truncate"] = True
    url = f"{base}/{SECRET}/{REPO}.git/git-upload-pack"
    with pytest.raises(ClientPayloadError):
        async with session.post(url, data=b"0000") as resp:
            await resp.read()
    assert "transfer of Owner/My-Repo interrupted" in caplog.text
    status["truncate"] = False
    async with session.post(url, data=b"0000") as resp:
        assert resp.status == 200
        assert await resp.read() == b"PACK0000"


async def test_client_disconnect_is_not_an_upstream_error(relay, upstream, caplog):
    """The Supervisor leaving mid-transfer is logged at debug, not as a failure."""
    base, _, session = relay
    _, _, status = upstream
    status["slow"] = True
    status["slow_done"] = asyncio.Event()
    caplog.set_level(logging.DEBUG)
    url = f"{base}/{SECRET}/{REPO}.git/git-upload-pack"
    resp = await session.post(url, data=b"0000")
    assert resp.status == 200
    await resp.content.readany()
    resp.close()  # drop the connection mid-body
    await asyncio.wait_for(status["slow_done"].wait(), timeout=10)
    await asyncio.sleep(0.1)
    assert "client stopped reading Owner/My-Repo" in caplog.text
    assert "interrupted" not in caplog.text
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    status["slow"] = False
    async with session.post(url, data=b"0000") as resp:
        assert resp.status == 200
        assert await resp.read() == b"PACK0000"

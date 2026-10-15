"""httpx and aiohttp under a secured tool, sync and async: their requests are
checked at the wire, and the permission engine's own lookups made from
inside a check are not. Needs httpx and aiohttp; skipped without them."""

from __future__ import annotations

import asyncio
import json
import urllib.request
from collections.abc import Iterator
from typing import Any

import pytest

from tests import harness
from tests.harness.wire import Engine, requests_seen
from toolpass import ActionRefused, Session, Toolkit

httpx = pytest.importorskip("httpx")
aiohttp = pytest.importorskip("aiohttp")


@pytest.fixture
def gh() -> Iterator[harness.Server]:
    with harness.Server(tls=False) as srv:
        srv.json("GET", "/api/v3/repos/acme/x", 200, {"full_name": "acme/x"})
        srv.json("POST", "/api/v3/repos/acme/x/pulls", 201, {"number": 7})
        srv.json("POST", "/check", 200, {"decision": "deny", "reason": "no"})
        yield srv


class RemoteLikeEngine(Engine):
    """An engine whose check itself makes an HTTP request (as Toolpass.remote does, over urllib)."""

    def __init__(self, github_url: str, check_url: str) -> None:
        super().__init__(github_url)
        self.check_url = check_url

    def check(self, user: str, connection: str, action: str, resource: str, groups: Any = None, *, fresh: bool = False) -> Any:
        req = urllib.request.Request(
            self.check_url, data=json.dumps({"user": user, "action": action}).encode(), method="POST", headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=5):
            pass
        return super().check(user, connection, action, resource, groups, fresh=fresh)


@pytest.fixture
def engine(gh: harness.Server) -> RemoteLikeEngine:
    e = RemoteLikeEngine(gh.url, gh.url + "/check")
    e.allow[("dana@example.com", "repo.read")] = True
    e.allow[("admin@example.com", "repo.read")] = True
    e.allow[("admin@example.com", "pr.create")] = True
    return e


def test_httpx_sync(gh, engine, events):
    tools = Toolkit(audit=events, permissions=engine)

    @tools.tool()
    def propose(repo: str) -> int:
        with httpx.Client() as c:
            c.get(f"{gh.url}/api/v3/repos/{repo}")
            return int(c.post(f"{gh.url}/api/v3/repos/{repo}/pulls", json={"title": "t"}).json()["number"])

    with Session("dana@example.com").active(), pytest.raises(ActionRefused) as e:
        propose("acme/x")
    assert e.value.code == "not_authorized"
    assert requests_seen(gh) == ["POST /check", "GET /api/v3/repos/acme/x", "POST /check"]  # the engine's lookups went out unchecked
    assert [a["action"] for a in events.last.actions] == ["github:repo.read", "github:pr.create"]
    with Session("admin@example.com").active():
        assert propose("acme/x") == 7


def test_httpx_async(gh, engine, events):
    tools = Toolkit(
        audit=events,
        permissions=engine,
        entitlements={"tools": {"propose": {"actions": [{"action": "github:pr.create", "resource": "repo:acme/*", "as": "user"}]}}},
    )

    @tools.tool()
    async def propose(repo: str) -> int:
        async with httpx.AsyncClient() as c:
            r = await c.post(f"{gh.url}/api/v3/repos/{repo}/pulls", json={"title": "t"})
            return int(r.json()["number"])

    with Session("dana@example.com").active(), pytest.raises(ActionRefused) as e:
        asyncio.run(propose("acme/x"))
    assert e.value.code == "not_authorized"  # not unknown_host: the engine's own /check call was not attributed to the tool
    assert requests_seen(gh) == ["POST /check"]
    with Session("admin@example.com").active():
        assert asyncio.run(propose("acme/x")) == 7


def test_aiohttp(gh, engine, events):
    tools = Toolkit(
        audit=events,
        permissions=engine,
        entitlements={"tools": {"propose": {"actions": [{"action": "github:pr.create", "resource": "repo:acme/*", "as": "user"}]}}},
    )

    @tools.tool()
    async def propose(repo: str) -> int:
        async with aiohttp.ClientSession() as s, s.post(f"{gh.url}/api/v3/repos/{repo}/pulls", json={"title": "t"}) as r:
            return int((await r.json())["number"])

    with Session("dana@example.com").active(), pytest.raises(ActionRefused) as e:
        asyncio.run(propose("acme/x"))
    assert e.value.code == "not_authorized"
    assert requests_seen(gh) == ["POST /check"]
    with Session("admin@example.com").active():
        assert asyncio.run(propose("acme/x")) == 7
    assert [a["action"] for a in events.last.actions] == ["github:pr.create"]

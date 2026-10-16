"""The demo's GitHub stand-in: the real github integration, in a real
in-process engine, gets the answers world.py describes from it, and the
requests the demo's tools make route to the actions those answers are for."""

from __future__ import annotations

import json
import sys
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

_DEMO = str(Path(__file__).resolve().parents[2] / "examples" / "demo")
if _DEMO not in sys.path:
    sys.path.insert(0, _DEMO)

from fake_github import CONNECTION_ID, GitHubFake, connection  # noqa: E402
from world import DANA, SAM  # noqa: E402

from toolpass import Toolpass  # noqa: E402
from toolpass.core.routes import Request, host_key  # noqa: E402
from toolpass.integrations.github.routes import ROUTES  # noqa: E402

NOBODY = "nobody@acme.dev"


@pytest.fixture
def fake() -> Iterator[GitHubFake]:
    f = GitHubFake()
    f.start()
    yield f
    f.stop()


@pytest.fixture
def tp(fake: GitHubFake, tmp_path: Path) -> Toolpass:
    return Toolpass(connections=[connection(fake.url, tmp_path)])


def route(fake: GitHubFake, method: str, path: str, body: Any = None) -> tuple[str | None, str | None]:
    """What the wire hook would check for a tool's request: the connection's
    longest matching endpoint prefix stripped, then the route table."""
    host, port = "127.0.0.1", int(fake.url.rsplit(":", 1)[1])
    key = host_key(host, port, "http")
    prefixes = [p for h, p in ROUTES.endpoints({"url": fake.url}) if h == key and (path == p or path.startswith(p + "/"))]
    assert prefixes, path
    raw = json.dumps(body).encode() if body is not None else None
    o = ROUTES.match(Request("http", host, port, method, path, "", {"content-type": "application/json"}, raw), max(prefixes, key=len))
    assert o is not None, path
    return o.action, o.resource


def call(fake: GitHubFake, method: str, path: str, body: Any = None) -> tuple[int, Any]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(fake.url + path, data=data, method=method, headers={"Content-Type": "application/json", "Authorization": "Bearer agent"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_repo_metadata(fake: GitHubFake, tp: Toolpass) -> None:
    assert route(fake, "GET", "/api/v3/repos/acme/shop") == ("repo.read", "repo:acme/shop")
    status, repo = call(fake, "GET", "/api/v3/repos/acme/shop")
    assert status == 200
    assert repo["full_name"] == "acme/shop" and repo["default_branch"] == "main"
    assert tp.check(DANA, CONNECTION_ID, "repo.read", "repo:acme/shop").decision == "allow"


def test_open_pull_request(fake: GitHubFake, tp: Toolpass) -> None:
    pr = {"title": "Fix Safari checkout", "head": "fix/safari", "base": "main", "body": "Fixes SHOP-12"}
    assert route(fake, "POST", "/api/v3/repos/acme/shop/pulls", pr) == ("pr.create", "repo:acme/shop")
    assert tp.check(DANA, CONNECTION_ID, "pr.create", "repo:acme/shop").decision == "allow"
    sam = tp.check(SAM, CONNECTION_ID, "pr.create", "repo:acme/shop")
    assert sam.decision == "deny" and "forking disabled" in sam.reason
    assert tp.check(NOBODY, CONNECTION_ID, "pr.create", "repo:acme/shop").decision == "deny"

    status, made = call(fake, "POST", "/api/v3/repos/acme/shop/pulls", pr)
    assert status == 201
    assert made["number"] == 1 and made["html_url"].endswith("/acme/shop/pull/1")
    status, listed = call(fake, "GET", "/api/v3/repos/acme/shop/pulls")
    assert status == 200 and [p["title"] for p in listed] == ["Fix Safari checkout"]


def test_list_pull_requests(fake: GitHubFake, tp: Toolpass) -> None:
    assert route(fake, "GET", "/api/v3/repos/acme/shop/pulls") == ("repo.read", "repo:acme/shop")
    assert tp.check(DANA, CONNECTION_ID, "repo.read", "repo:acme/shop").decision == "allow"
    assert tp.check(SAM, CONNECTION_ID, "repo.read", "repo:acme/shop").decision == "allow"
    assert call(fake, "GET", "/api/v3/repos/acme/shop/pulls") == (200, [])


def test_comment_on_pull_request(fake: GitHubFake, tp: Toolpass) -> None:
    # GitHub lets anyone with read access comment, and the route table says so:
    # a comment is repo.read, so sam may comment too.
    assert route(fake, "POST", "/api/v3/repos/acme/shop/issues/1/comments", {"body": "x"}) == ("repo.read", "repo:acme/shop")
    assert tp.check(DANA, CONNECTION_ID, "repo.read", "repo:acme/shop").decision == "allow"
    assert tp.check(SAM, CONNECTION_ID, "repo.read", "repo:acme/shop").decision == "allow"

    assert call(fake, "POST", "/api/v3/repos/acme/shop/issues/1/comments", {"body": "x"})[0] == 404
    call(fake, "POST", "/api/v3/repos/acme/shop/pulls", {"title": "t", "head": "h", "base": "main"})
    status, c = call(fake, "POST", "/api/v3/repos/acme/shop/issues/1/comments", {"body": "Looks good"})
    assert status == 201 and c["body"] == "Looks good" and "#issuecomment-" in c["html_url"]
    assert call(fake, "GET", "/api/v3/repos/acme/shop/issues/1/comments")[1] == [c]


def test_unknown_email_is_denied(tp: Toolpass) -> None:
    d = tp.check(NOBODY, CONNECTION_ID, "repo.read", "repo:acme/shop")
    assert d.decision == "deny", d.reason


def test_toolpass_calls_use_the_app_credential(fake: GitHubFake, tp: Toolpass) -> None:
    assert tp.check(DANA, CONNECTION_ID, "repo.push", "repo:acme/shop").decision == "allow"
    assert tp.check(SAM, CONNECTION_ID, "repo.push", "repo:acme/shop").decision == "deny"
    assert "POST /api/v3/app/installations/42/access_tokens" in fake.seen
    assert "POST /api/graphql" in fake.seen
    assert {p[0]: p[1:] for p in tp.probe()}[CONNECTION_ID] == (True, "app toolpass-reader installed in acme")

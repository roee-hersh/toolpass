"""The demo's stand-in Jira site gives toolpass's real jira integration the
answers world.py implies, and the tool endpoints work."""

from __future__ import annotations

import json
import sys
import urllib.parse
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

DEMO = str(Path(__file__).resolve().parents[2] / "examples" / "demo")
if DEMO not in sys.path:
    sys.path.insert(0, DEMO)

from fake_jira import JiraFake, connection  # noqa: E402
from world import DANA, SAM  # noqa: E402

from toolpass import Toolpass  # noqa: E402
from toolpass.core.integration import Settings  # noqa: E402
from toolpass.core.routes import Request  # noqa: E402
from toolpass.integrations.jira.routes import ROUTES  # noqa: E402

CONN = "jira-acme"


@pytest.fixture
def env(tmp_path: Path) -> Iterator[tuple[JiraFake, Toolpass, dict[str, Any]]]:
    fake = JiraFake()
    url = fake.start()
    conn = connection(url, tmp_path)
    try:
        yield fake, Toolpass(connections=[conn]), conn
    finally:
        fake.stop()


def route(conn: dict[str, Any], method: str, path: str, body: Any = None) -> tuple[str | None, str]:
    """The action and resource the wire hook would check for the request."""
    u = urllib.parse.urlsplit(conn["url"] + path)
    [(_, prefix)] = ROUTES.endpoints(Settings(conn["id"], "jira", {"url": conn["url"]}))
    raw = json.dumps(body).encode() if body is not None else None
    o = ROUTES.match(Request(u.scheme, u.hostname or "", u.port, method, u.path, "", {"content-type": "application/json"}, raw), prefix)
    assert o is not None and o.unresolved is None, o
    return o.action, o.resource


def call(url: str, method: str, path: str, body: Any = None) -> tuple[int, Any]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url + path, data, {"Content-Type": "application/json", "Authorization": "Bearer tool"}, method=method)
    with urllib.request.urlopen(req) as resp:
        raw = resp.read()
        return resp.status, json.loads(raw) if raw else None


COMMENT = {"body": {"type": "doc", "version": 1, "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Looking."}]}]}}
CREATE = {"fields": {"project": {"key": "SHOP"}, "summary": "Cart total off by one cent", "issuetype": {"name": "Bug"}, "description": "Seen on staging."}}


@pytest.mark.parametrize(
    ("method", "path", "body", "action", "resource", "dana", "sam"),
    [
        ("GET", "/rest/api/3/issue/SHOP-12", None, "BROWSE_PROJECTS", "issue:SHOP-12", "allow", "allow"),
        ("POST", "/rest/api/3/issue/SHOP-12/comment", COMMENT, "ADD_COMMENTS", "issue:SHOP-12", "allow", "deny"),
        ("GET", "/rest/api/3/issue/SHOP-12/transitions", None, "BROWSE_PROJECTS", "issue:SHOP-12", "allow", "allow"),
        ("POST", "/rest/api/3/issue/SHOP-12/transitions", {"transition": {"id": "31"}}, "TRANSITION_ISSUES", "issue:SHOP-12", "allow", "deny"),
        ("POST", "/rest/api/3/issue", CREATE, "CREATE_ISSUES", "project:SHOP", "allow", "deny"),
    ],
)
def test_tool_requests_are_checked_against_world(env, method, path, body, action, resource, dana, sam):
    _, tp, conn = env
    assert route(conn, method, path, body) == (action, resource)
    for user, want in ((DANA, dana), (SAM, sam)):
        d = tp.check(user, CONN, action, resource)
        assert d.decision == want, (user, d)


def test_issue_and_comments(env):
    fake, _, _ = env
    status, issue = call(fake.url, "GET", "/rest/api/3/issue/SHOP-12")
    assert status == 200 and issue["key"] == "SHOP-12" and issue["fields"]["summary"] == "Checkout button does nothing on Safari"
    assert call(fake.url, "POST", "/rest/api/3/issue/SHOP-12/comment", COMMENT)[0] == 201
    assert call(fake.url, "POST", "/rest/api/3/issue/SHOP-12/comment", {"body": "plain text"})[0] == 201
    _, issue = call(fake.url, "GET", "/rest/api/3/issue/SHOP-12")
    assert issue["fields"]["comment"]["total"] == 2


def test_transitions_change_the_status(env):
    fake, _, _ = env
    _, out = call(fake.url, "GET", "/rest/api/3/issue/SHOP-12/transitions")
    names = {t["name"]: t["id"] for t in out["transitions"]}
    assert names["In Review"] == "31" and names["Done"] == "41"
    assert call(fake.url, "POST", "/rest/api/3/issue/SHOP-12/transitions", {"transition": {"id": "31"}})[0] == 204
    _, issue = call(fake.url, "GET", "/rest/api/3/issue/SHOP-12")
    assert issue["fields"]["status"]["name"] == "In Review"


def test_create_takes_the_next_number_and_is_checkable(env):
    fake, tp, _ = env
    status, out = call(fake.url, "POST", "/rest/api/3/issue", CREATE)
    assert status == 201 and out["key"] == "SHOP-15" and out["id"]
    _, issue = call(fake.url, "GET", "/rest/api/3/issue/SHOP-15")
    assert issue["fields"]["summary"] == CREATE["fields"]["summary"] and issue["fields"]["issuetype"]["name"] == "Bug"
    assert tp.check(DANA, CONN, "EDIT_ISSUES", "issue:SHOP-15").allowed
    assert call(fake.url, "POST", "/rest/api/3/issue", CREATE)[1]["key"] == "SHOP-16"


def test_an_unknown_email_is_denied(env):
    _, tp, _ = env
    d = tp.check("nobody@acme.dev", CONN, "BROWSE_PROJECTS", "issue:SHOP-12")
    assert d.decision == "deny" and d.code == "user_not_found", d


def test_the_connection_probes_clean(env):
    _, tp, _ = env
    [(_, ok, summary)] = tp.probe()
    assert ok and "Toolpass Admin" in summary, summary

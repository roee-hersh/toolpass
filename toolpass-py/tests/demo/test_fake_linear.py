"""The demo's Linear stand-in: the route table reads the tools' GraphQL
requests as toolpass's wire hook would, the real linear integration answers
the checks from the fake, and the tools' own operations work."""

from __future__ import annotations

import json
import sys
import urllib.parse
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

_DEMO = str(Path(__file__).resolve().parents[2] / "examples" / "demo")
if _DEMO not in sys.path:
    sys.path.insert(0, _DEMO)

from fake_linear import TEAM_IDS, LinearFake, connection  # noqa: E402
from world import DANA, SAM  # noqa: E402

from toolpass import Toolpass  # noqa: E402
from toolpass._wire import Connections  # noqa: E402
from toolpass.core.routes import Request  # noqa: E402
from toolpass.integrations.linear.routes import ROUTES  # noqa: E402

CONN = "linear-acme"
ENG = TEAM_IDS["ENG"]

CREATE_INLINE = (
    f'mutation {{ issueCreate(input: {{teamId: "{ENG}", title: "Checkout fails", description: "on Safari"}}) '
    "{ success issue { id identifier url title } } }"
)
CREATE_VARS = "mutation Create($input: IssueCreateInput!) { issueCreate(input: $input) { success issue { id identifier url title } } }"
LIST = f'query {{ issues(filter: {{team: {{id: {{eq: "{ENG}"}}}}}}) {{ nodes {{ identifier title }} }} }}'


@pytest.fixture
def env(tmp_path: Path) -> Iterator[tuple[LinearFake, Toolpass, dict[str, Any]]]:
    fake = LinearFake()
    conn = connection(fake.start(), tmp_path)
    tp = Toolpass(connections=[conn])
    try:
        yield fake, tp, conn
    finally:
        fake.stop()


def post(conn: dict[str, Any], query: str, variables: dict[str, Any] | None = None) -> tuple[Request, dict[str, Any]]:
    """Send one GraphQL request to the fake as a tool would; also return
    the request as the wire hook sees it."""
    body = json.dumps({"query": query, "variables": variables} if variables else {"query": query}).encode()
    u = urllib.parse.urlsplit(conn["url"])
    headers = {"content-type": "application/json", "authorization": "lin_api_tool"}
    seen = Request(scheme=u.scheme, host=u.hostname or "", port=u.port, method="POST", raw_path=u.path, headers=headers, body=body)
    req = urllib.request.Request(conn["url"], data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=10) as resp:
        return seen, json.loads(resp.read())


def observed(tp: Toolpass, r: Request) -> tuple[str | None, str | None, str]:
    found = Connections.from_toolpass(tp).resolve(r)
    assert found is not None, "the request is not attributed to linear-acme"
    c, prefix = found
    assert c.id == CONN and prefix == "/graphql"
    o = ROUTES.match(r, prefix)
    assert o is not None
    assert o.unresolved is None, o.unresolved
    return o.action, o.resource, o.effect


@pytest.mark.parametrize("variables", [False, True], ids=["inline", "variables"])
def test_issue_create(env: tuple[LinearFake, Toolpass, dict[str, Any]], variables: bool) -> None:
    _, tp, conn = env
    if variables:
        r, out = post(conn, CREATE_VARS, {"input": {"teamId": ENG, "title": "Checkout fails", "description": "on Safari"}})
    else:
        r, out = post(conn, CREATE_INLINE)
    assert observed(tp, r) == ("team.member", f"team:{ENG}", "write")
    assert tp.check(DANA, CONN, "team.member", f"team:{ENG}").decision == "allow"
    d = tp.check(SAM, CONN, "team.member", f"team:{ENG}")
    assert d.decision == "deny", d.reason
    issue = out["data"]["issueCreate"]["issue"]
    assert out["data"]["issueCreate"]["success"] is True
    assert issue["identifier"] == "ENG-1" and issue["title"] == "Checkout fails" and issue["url"].endswith("/eng-1")


def test_team_by_key_and_view(env: tuple[LinearFake, Toolpass, dict[str, Any]]) -> None:
    _, tp, _ = env
    assert tp.check(DANA, CONN, "team.member", "team:ENG").decision == "allow"
    assert tp.check(SAM, CONN, "team.member", "team:ENG").decision == "deny"
    # ENG is public: every workspace member sees it.
    assert tp.check(SAM, CONN, "team.view", "team:ENG").decision == "allow"


def test_list_issues(env: tuple[LinearFake, Toolpass, dict[str, Any]]) -> None:
    _, tp, conn = env
    post(conn, CREATE_INLINE)
    post(conn, CREATE_VARS, {"input": {"teamId": ENG, "title": "Add Apple Pay"}})
    r, out = post(conn, LIST)
    assert observed(tp, r) == (None, "", "read")  # a read with no check
    assert [(n["identifier"], n["title"]) for n in out["data"]["issues"]["nodes"]] == [("ENG-1", "Checkout fails"), ("ENG-2", "Add Apple Pay")]
    # A created issue is visible to the integration's issue checks too.
    assert tp.check(SAM, CONN, "issue.view", "issue:ENG-2").decision == "allow"
    assert tp.check(DANA, CONN, "issue.edit", "issue:ENG-1").decision == "allow"


def test_unknown_email_is_denied(env: tuple[LinearFake, Toolpass, dict[str, Any]]) -> None:
    _, tp, _ = env
    d = tp.check("nobody@acme.dev", CONN, "team.member", f"team:{ENG}")
    assert d.decision == "deny" and d.code == "user_not_found", d.reason


def test_probe(env: tuple[LinearFake, Toolpass, dict[str, Any]]) -> None:
    _, tp, _ = env
    [(cid, ok, text)] = tp.probe()
    assert cid == CONN and ok, text

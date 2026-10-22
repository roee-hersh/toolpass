"""PyGithub under a secured tool: its requests (requests -> urllib3) are
checked at the wire. Needs PyGithub; skipped without it."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from tests import harness
from tests.harness.wire import Engine, requests_seen
from toolpass import ActionRefused, Session, Toolkit, without_toolpass

github = pytest.importorskip("github")


@pytest.fixture
def gh() -> Iterator[harness.Server]:
    with harness.Server(tls=False) as srv:
        srv.json("GET", "/api/v3/repos/acme/x", 200, {"full_name": "acme/x", "name": "x", "owner": {"login": "acme"}, "url": srv.url + "/api/v3/repos/acme/x"})
        srv.json(
            "GET",
            "/api/v3/repos/acme/x/contents/README.md",
            200,
            {"type": "file", "encoding": "base64", "content": "aGVsbG8=", "path": "README.md", "name": "README.md", "sha": "a", "size": 5, "url": srv.url},
        )
        srv.json("POST", "/api/v3/repos/acme/x/pulls", 201, {"number": 7, "url": srv.url + "/api/v3/repos/acme/x/pulls/7", "title": "t"})
        yield srv


@pytest.fixture
def engine(gh: harness.Server) -> Engine:
    e = Engine(gh.url)
    e.allow[("dana@example.com", "repo.read")] = True
    e.allow[("admin@example.com", "repo.read")] = True
    e.allow[("admin@example.com", "pr.create")] = True
    return e


def client(gh: harness.Server) -> Any:
    return github.Github(auth=github.Auth.Token("ghp_" + harness.CANARY), base_url=gh.url + "/api/v3", retry=None)


def test_a_denied_pull_request_never_leaves_the_process(gh, engine, events):
    tools = Toolkit(audit=events, permissions=engine)

    @tools.tool()
    def propose(repo: str, title: str) -> int:
        g = client(gh)
        r = g.get_repo(repo)  # GET /repos/acme/x: repo.read, allowed
        pr = r.create_pull(title=title, body="", head="f", base="main")  # pr.create: refused
        return int(pr.number)

    with Session("dana@example.com").active(), pytest.raises(ActionRefused) as e:
        propose("acme/x", "fix")
    assert e.value.code == "not_authorized"
    assert requests_seen(gh) == ["GET /api/v3/repos/acme/x"]
    assert [a["action"] for a in events.last.actions] == ["github:repo.read", "github:pr.create"]
    assert harness.CANARY not in str(events.last.to_dict())

    with Session("admin@example.com").active():
        assert propose("acme/x", "fix") == 7
    assert requests_seen(gh)[-1] == "POST /api/v3/repos/acme/x/pulls"


def test_without_toolpass_reads_then_the_user_writes(gh, engine, events):
    tools = Toolkit(audit=events, permissions=engine)
    engine.allow[("bob@example.com", "pr.create")] = True

    @tools.tool()
    def propose(repo: str) -> int:
        g = client(gh)
        with without_toolpass():
            readme = g.get_repo(repo, lazy=True).get_contents("README.md")
        assert readme.decoded_content == b"hello"
        return int(g.get_repo(repo, lazy=True).create_pull(title="t", body="", head="f", base="main").number)

    with Session("bob@example.com").active():
        assert propose("acme/x") == 7
    assert engine.asked == [("bob@example.com", "github-acme", "pr.create", "repo:acme/x")]
    assert [a.get("as_agent", False) for a in events.last.actions] == [True, False]

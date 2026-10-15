"""Edges of the hook and the entitlements found in review: each one a case
that was wrong once."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest

from tests import harness
from tests.harness.wire import Engine, http, requests_seen
from toolpass import ActionRefused, Entitlement, Session, Toolkit
from toolpass._wire import Connections
from toolpass.core.integration import Settings
from toolpass.core.routes import Request, endpoint_of_url, host_key
from toolpass.integrations.github import INTEGRATION as GITHUB
from toolpass.integrations.github.routes import ROUTES as GITHUB_ROUTES
from toolpass.integrations.slack.routes import ROUTES as SLACK_ROUTES


@pytest.fixture
def gh() -> Iterator[harness.Server]:
    with harness.Server(tls=False) as srv:
        srv.json("GET", "/api/v3/repos/acme/x", 200, {"full_name": "acme/x"})
        srv.json("GET", "/api/v3/repos/acme/secret", 200, {"full_name": "acme/secret"})
        srv.json("POST", "/api/v3/repos/acme/x/pulls", 201, {"number": 7})
        yield srv


@pytest.fixture
def engine(gh: harness.Server) -> Engine:
    e = Engine(gh.url)
    e.allow[("admin@example.com", "repo.read")] = True
    e.allow[("admin@example.com", "pr.create")] = True
    return e


def test_a_glob_in_an_argument_does_not_widen_an_exact_entitlement(gh, engine, events):
    ents = {"tools": {"look": {"actions": [{"action": "github:repo.read", "resource": "repo:{repo}", "as": "agent", "effect": "read"}]}}}
    tools = Toolkit(audit=events, permissions=engine, entitlements=ents)

    @tools.tool()
    def look(repo: str) -> Any:
        return http("GET", f"{gh.url}/api/v3/repos/acme/secret")  # not the repo the model named

    with Session("dana@example.com").active(), pytest.raises(ActionRefused) as e:
        look("acme/*")
    assert e.value.code == "unentitled_action"
    assert requests_seen(gh) == []
    e2 = Entitlement("github:repo.read", "repo:{owner}/*")
    assert e2.matches("repo:acme/x", {"owner": "acme"})
    assert not e2.matches("repo:evil/x", {"owner": "*"})  # the value is literal, the template's own * is the glob
    assert not Entitlement("github:repo.read", "repo:{repo}").matches("repo:acme/secret", {"repo": "acme/*"})


def test_a_multipart_body_with_a_binary_part_still_yields_its_fields():
    boundary = "b"
    parts = [
        b'Content-Disposition: form-data; name="channels"\r\n\r\nC0123456789\r\n',
        b'Content-Disposition: form-data; name="file"; filename="chart.png"\r\nContent-Type: image/png\r\n\r\n\x89PNG\r\n\x1a\n\xff\xfe\r\n',
    ]
    body = b"".join(f"--{boundary}\r\n".encode() + p for p in parts) + f"--{boundary}--\r\n".encode()
    r = Request("https", "slack.com", 443, "POST", "/api/files.upload", "", {"content-type": f"multipart/form-data; boundary={boundary}"}, body)
    assert r.form() == {"channels": "C0123456789"}
    o = SLACK_ROUTES.match(r)
    assert o is not None and o.resource == "channel:C0123456789"


def test_two_connections_of_one_integration_defer_the_check_to_the_wire(gh, engine, events):
    ents = {"tools": {"propose": {"actions": [{"action": "github:pr.create", "resource": "repo:{repo}", "as": "user", "effect": "write"}]}}}
    two = Engine(gh.url)
    two.allow.update(engine.allow)
    original = two._describe

    def describe() -> list[tuple[str, Any, Any]]:
        return [*original(), ("github-other", GITHUB, Settings("github-other", "github", {"url": "https://ghe.example.net", "organization": "other"}))]

    two._describe = describe  # type: ignore[method-assign]
    tools = Toolkit(audit=events, permissions=two, entitlements=ents)

    @tools.tool()
    def propose(repo: str) -> str:
        http("POST", f"{gh.url}/api/v3/repos/{repo}/pulls", {"title": "t"})
        return "opened"

    with Session("admin@example.com").active():
        assert propose("acme/x") == "opened"
    assert two.asked == [("admin@example.com", "github-acme", "pr.create", "repo:acme/x")]  # checked at the wire, against the right connection
    with Session("dana@example.com").active(), pytest.raises(ActionRefused) as e:
        propose("acme/x")
    assert e.value.code == "not_authorized"


def test_an_encoded_slash_stays_inside_its_segment():
    r = Request("https", "api.github.com", 443, "DELETE", "/repos/acme/x%2Fy", "", {}, None)
    o = GITHUB_ROUTES.match(r)
    assert o is not None and o.action == "repo.admin" and o.resource == "repo:acme/x/y" and o.effect == "destructive"
    r = Request("https", "api.github.com", 443, "GET", "/repos/acme/x/contents/a%2Fb.txt", "", {}, None)
    o = GITHUB_ROUTES.match(r)
    assert o is not None and o.resource == "repo:acme/x"


def test_ipv6_hosts_match_with_or_without_brackets():
    assert host_key("[::1]", 8443) == host_key("::1", 8443) == "::1:8443"
    assert endpoint_of_url("https://[::1]:8443/") == ("::1:8443", "")
    conns = Connections.from_mappings([{"id": "gh", "integration": "github", "url": "https://[::1]:8443", "organization": "acme"}])
    hit = conns.resolve(Request("https", "[::1]", 8443, "GET", "/api/v3/repos/acme/x", "", {}, None))
    assert hit is not None and hit[0].id == "gh" and hit[1] == "/api/v3"


def test_routes_cli_effects_match_the_hooks(capsys):
    from toolpass import cli

    assert cli.run(["routes", "github"]) == 0
    out = capsys.readouterr().out
    [line] = [ln for ln in out.splitlines() if "/repos/{owner}/{repo}/git/refs/{ref+}" in ln and ln.strip().startswith("DELETE")]
    assert line.rstrip().endswith("destructive  (resource from the body)")
    assert json.dumps(out)  # printable

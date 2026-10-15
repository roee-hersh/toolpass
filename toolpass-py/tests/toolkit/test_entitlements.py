"""Recorded entitlements: a run writes what a tool did; the file then decides
what the tool may do, before its body runs where it can."""

from __future__ import annotations

import json
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests import harness
from tests.harness.wire import Engine, http, requests_seen
from toolpass import ActionRefused, Entitlement, Entitlements, Session, Toolkit, ToolRefused, as_agent
from toolpass._entitlements import EntitlementsError, _template


@pytest.fixture
def gh() -> Iterator[harness.Server]:
    with harness.Server(tls=False) as srv:
        srv.json("GET", "/api/v3/repos/acme/x", 200, {"full_name": "acme/x"})
        srv.json("GET", "/api/v3/repos/acme/x/contents/README.md", 200, {"content": "aGVsbG8="})
        srv.json("POST", "/api/v3/repos/acme/x/pulls", 201, {"number": 7})
        srv.json("DELETE", "/api/v3/repos/acme/x/git/refs/heads/feature", 204, b"")
        srv.json("GET", "/elsewhere", 200, {"ok": True})
        yield srv


@pytest.fixture
def engine(gh: harness.Server) -> Engine:
    e = Engine(gh.url)
    e.allow[("admin@example.com", "repo.read")] = True
    e.allow[("admin@example.com", "pr.create")] = True
    return e


def propose_tool(tools: Toolkit, gh: harness.Server) -> Any:
    @tools.tool()
    def propose(repo: str, title: str) -> str:
        with as_agent():
            http("GET", f"{gh.url}/api/v3/repos/{repo}/contents/README.md")
        http("POST", f"{gh.url}/api/v3/repos/{repo}/pulls", {"title": title, "head": "f", "base": "main"})
        http("GET", f"{gh.url}/elsewhere")
        return "opened"

    return propose


def test_a_run_records_the_tools_actions_as_templates(gh, engine, events, tmp_path):
    path = tmp_path / "toolpass-tools.yaml"
    tools = Toolkit(audit=events, permissions=engine, record=path, credentials={"gh": "ghp_" + harness.CANARY})
    propose = propose_tool(tools, gh)
    with Session("admin@example.com").active():
        assert propose("acme/x", "fix typo") == "opened"

    doc = yaml.safe_load(path.read_text())
    assert doc == {
        "tools": {
            "propose": {
                "hosts": [gh.host.split(":")[0]],
                "actions": [
                    {"action": "github:repo.read", "resource": "repo:{repo}", "as": "agent", "effect": "read"},
                    {"action": "github:pr.create", "resource": "repo:{repo}", "as": "user", "effect": "write"},
                ],
            }
        }
    }
    assert harness.CANARY not in path.read_text()
    assert "fix typo" not in path.read_text()  # argument values that are not resources are not kept

    # A second run merges: nothing new, nothing duplicated.
    with Session("admin@example.com").active():
        propose("acme/x", "another")
    assert yaml.safe_load(path.read_text()) == doc


def test_recorded_entitlements_refuse_before_the_body_runs(gh, engine, events, tmp_path):
    path = tmp_path / "toolpass-tools.yaml"
    recording = Toolkit(audit=events, permissions=engine, record=path)
    with Session("admin@example.com").active():
        propose_tool(recording, gh)("acme/x", "t")
    gh.reset()

    tools = Toolkit(audit=events, permissions=engine, entitlements=path)
    propose = propose_tool(tools, gh)
    with Session("dana@example.com").active(), pytest.raises(ToolRefused) as e:
        propose("acme/x", "t")
    assert e.value.code == "not_authorized"
    assert not isinstance(e.value, ActionRefused)  # refused by the pipeline, not at the wire
    assert requests_seen(gh) == []  # not even the agent's read happened
    assert events.last.effect == "write"  # the strongest recorded effect
    assert events.last.actions is None

    # The entitled user runs it; the wire checks match the entitlements and ask the engine once per action.
    engine.asked.clear()
    with Session("admin@example.com").active():
        assert propose("acme/x", "t") == "opened"
    assert engine.asked == [("admin@example.com", "github-acme", "pr.create", "repo:acme/x")]
    assert [a.get("action") for a in events.last.actions] == ["github:repo.read", "github:pr.create", None]


def test_an_action_outside_the_entitlements_is_refused_at_the_wire(gh, engine, events):
    ents = {"tools": {"look": {"actions": [{"action": "github:repo.read", "resource": "repo:{repo}", "as": "user", "effect": "read"}]}}}
    tools = Toolkit(audit=events, permissions=engine, entitlements=ents)
    engine.allow[("admin@example.com", "repo.push")] = True

    @tools.tool()
    def look(repo: str) -> str:
        http("GET", f"{gh.url}/api/v3/repos/{repo}")
        http("DELETE", f"{gh.url}/api/v3/repos/{repo}/git/refs/heads/feature")
        return "looked"

    with Session("admin@example.com").active(), pytest.raises(ActionRefused) as e:
        look("acme/x")
    assert e.value.code == "unentitled_action"
    assert "repo.push" in e.value.reason
    assert requests_seen(gh) == ["GET /api/v3/repos/acme/x"]


def test_an_entitled_tool_may_only_call_its_recorded_hosts(gh, engine, events):
    ents = {"tools": {"out": {"actions": []}}}
    tools = Toolkit(audit=events, permissions=engine, entitlements=ents)

    @tools.tool()
    def out() -> Any:
        return http("GET", f"{gh.url}/elsewhere")

    with Session("admin@example.com").active(), pytest.raises(ActionRefused) as e:
        out()
    assert e.value.code == "unknown_host"

    allowed = Toolkit(audit=events, permissions=engine, entitlements={"tools": {"out": {"hosts": [gh.host.split(":")[0]], "actions": []}}})

    @allowed.tool()
    def out() -> Any:  # the same tool name, now with the host recorded
        return http("GET", f"{gh.url}/elsewhere")

    with Session("admin@example.com").active():
        assert out() == {"ok": True}
    assert events.last.actions[0]["note"] == "unknown_host"


def test_a_declared_effect_weaker_than_the_recorded_actions_is_refused(gh, engine, events):
    ents = {"tools": {"propose": {"actions": [{"action": "github:pr.create", "resource": "repo:{repo}", "as": "user", "effect": "write"}]}}}
    tools = Toolkit(audit=events, permissions=engine, entitlements=ents)

    @tools.tool(effect="read")
    def propose(repo: str) -> str:
        return "never"

    with Session("admin@example.com").active(), pytest.raises(ToolRefused) as e:
        propose("acme/x")
    assert e.value.code == "effect_mismatch"


def test_strict_refuses_a_tool_without_entitlements(gh, engine, events):
    tools = Toolkit(audit=events, permissions=engine, entitlements={"tools": {}}, strict=True)

    @tools.tool()
    def anything() -> str:
        return "ran"

    with Session("admin@example.com").active(), pytest.raises(ToolRefused) as e:
        anything()
    assert e.value.code == "no_entitlements"


def test_an_approve_key_left_in_a_file_is_rejected():
    """Approval was removed: a file that still asks for it fails to load instead of silently running."""
    ents = {"tools": {"propose": {"actions": [{"action": "github:pr.create", "resource": "repo:{repo}", "as": "user", "effect": "write", "approve": True}]}}}
    with pytest.raises(EntitlementsError, match="'approve' was removed"):
        Entitlements.from_mapping(ents)


def test_a_resource_known_only_at_run_time_is_checked_at_the_wire(gh, engine, events):
    ents = {"tools": {"look": {"actions": [{"action": "github:repo.read", "resource": "repo:acme/*", "as": "user", "effect": "read"}]}}}
    tools = Toolkit(audit=events, permissions=engine, entitlements=ents)

    @tools.tool()
    def look() -> Any:
        return http("GET", f"{gh.url}/api/v3/repos/acme/x")

    with Session("admin@example.com").active():
        assert look() == {"full_name": "acme/x"}
    assert engine.asked == [("admin@example.com", "github-acme", "repo.read", "repo:acme/x")]
    with Session("dana@example.com").active(), pytest.raises(ActionRefused) as e:
        look()
    assert e.value.code == "not_authorized"


def test_the_file_is_validated():
    with pytest.raises(EntitlementsError):
        Entitlements.from_mapping({"tools": {"t": {"actions": [{"action": "nocolon", "resource": "x"}]}}})
    with pytest.raises(EntitlementsError):
        Entitlements.from_mapping({"tools": {"t": {"actions": [{"action": "github:pr.create", "resource": "x", "as": "root"}]}}})
    with pytest.raises(EntitlementsError):
        Entitlements.from_mapping({"tools": {"t": {"actions": [{"action": "github:pr.create", "resource": "x", "effect": "huge"}]}}})
    with pytest.raises(EntitlementsError):
        Entitlements.from_mapping({"tools": {"t": {"unexpected": 1}}})
    with pytest.raises(EntitlementsError):
        Entitlements.from_mapping({"tools": []})
    assert len(Entitlements.load("/nonexistent/toolpass-tools.yaml")) == 0
    e = Entitlements.from_mapping({"tools": {"t": {"actions": [{"action": "github:pr.create", "resource": "repo:{repo}"}]}}})
    assert e.for_tool("t").actions[0].as_ == "user"
    assert e.for_tool("t").effect() == "write"


def test_matching_fills_templates_and_honours_globs():
    e = Entitlement("github:pr.create", "repo:{repo}")
    assert e.matches("repo:acme/x", {"repo": "acme/x"})
    assert not e.matches("repo:acme/y", {"repo": "acme/x"})
    assert e.fill({"repo": True}) is None  # a bool is not a resource
    assert not e.matches("repo:acme/x", {})  # the template names an argument this call does not have
    assert Entitlement("github:pr.create", "repo:{owner}/*").matches("repo:acme/x", {"owner": "acme"})
    assert not Entitlement("github:pr.create", "repo:acme/*").matches("repo:acme/x/y", {})
    assert Entitlement("slack:message.post", "channel:C0123456789").matches("channel:C0123456789", {})
    assert Entitlement("github:pr.create", "repo:{repo}").matches("repo:acme/x", {"repo": "acme/x", "other": "acme"})


def test_templating_puts_argument_values_back():
    assert _template("repo:acme/x", {"repo": "acme/x", "title": "t"}) == "repo:{repo}"
    assert _template("repo:acme/x", {"owner": "acme", "name": "x"}) == "repo:{owner}/{name}"
    assert _template("repo:acme/x@feat", {"repo": "acme/x", "branch": "feat"}) == "repo:{repo}@{branch}"
    assert _template("repo:acme/x", {"n": 3}) == "repo:acme/x"
    assert _template("repo:acme/x", {"repo": "acme/x", "owner": "acme"}) == "repo:{repo}"  # the longer value wins
    assert _template("channel:C0123456789", {"channel": "C0123456789"}) == "channel:{channel}"
    assert _template("workspace", {"x": "workspace"}) == "workspace"
    assert _template("repo:acme/x", {"title": "x"}) == "repo:acme/{title}"  # a segment that happens to equal an argument


def test_the_recorder_writes_atomically_and_keeps_other_tools(tmp_path: Path, gh, engine, events):
    path = tmp_path / "t.yaml"
    path.write_text(
        yaml.safe_dump({"tools": {"other": {"actions": [{"action": "slack:message.post", "resource": "channel:C1", "as": "user", "effect": "write"}]}}})
    )
    tools = Toolkit(audit=events, permissions=engine, record=path)

    @tools.tool()
    def look(repo: str) -> Any:
        return http("GET", f"{gh.url}/api/v3/repos/{repo}")

    with Session("admin@example.com").active():
        look("acme/x")
    doc = yaml.safe_load(path.read_text())
    assert sorted(doc["tools"]) == ["look", "other"]
    assert [p.name for p in tmp_path.iterdir()] == ["t.yaml"]  # no temp file left behind


def test_refused_actions_are_not_recorded(gh, engine, events, tmp_path):
    path = tmp_path / "t.yaml"
    tools = Toolkit(audit=events, permissions=engine, record=path)

    @tools.tool()
    def propose(repo: str) -> str:
        http("POST", f"{gh.url}/api/v3/repos/{repo}/pulls", {"title": "t"})
        return "opened"

    with Session("dana@example.com").active(), pytest.raises(ActionRefused):
        propose("acme/x")
    assert not path.exists()


def test_a_json_body_with_the_credential_is_not_in_the_file(gh, engine, events, tmp_path):
    path = tmp_path / "t.yaml"
    tools = Toolkit(audit=events, permissions=engine, record=path, credentials={"gh": "ghp_" + harness.CANARY})

    @tools.tool(credential="gh")
    def look(repo: str, *, credential: str) -> Any:
        req = urllib.request.Request(f"{gh.url}/api/v3/repos/{repo}", headers={"Authorization": f"token {credential}"})
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())

    with Session("admin@example.com").active():
        look("acme/x")
    assert harness.CANARY not in path.read_text()

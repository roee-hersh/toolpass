"""Three refusals a caller must tell apart: the user was checked and lacks
access (not_authorized), the question could not be answered
(authorization_error), nothing was checked because toolpass has no such
connection (not_configured). Only the first may say the user may not."""

from __future__ import annotations

import pytest

from toolpass import AuthDecision, Decision, Toolkit, Toolpass, ToolRefused, permission_check

TP = Toolpass(
    connections=[
        {"id": "things", "integration": "fake", "users": "dana@example.com"},
        {"id": "down", "integration": "fake", "users": "dana@example.com", "fail": "upstream_timeout"},
    ]
)


def tool_on(connection: str, action: str, resource: str = "thing:{key}", tp=TP):
    tools = Toolkit(audit=None)

    @tools.tool(effect="write", authorize=permission_check(tp, connection, action, resource))
    def write(key: str) -> str:
        return "ran"

    return write


def refused(fn, *args) -> ToolRefused:
    with pytest.raises(ToolRefused) as e:
        fn(*args)
    return e.value


def test_a_deny_is_not_authorized(dana):
    e = refused(tool_on("things", "thing.write"), "a")
    assert e.code == "not_authorized"
    assert "dana@example.com may not run write" in e.reason


@pytest.mark.parametrize(
    ("connection", "action", "resource"),
    [
        ("down", "thing.write", "thing:{key}"),  # upstream timeout
        ("things", "thing.write", "thing:broken"),  # unsupported
        ("things", "thing.write", "thing:hidden"),  # resource not visible
        ("things", "thing.write", "bad resource {key}"),  # invalid request
    ],
)
def test_an_unknown_answer_is_never_not_authorized(dana, connection, action, resource):
    e = refused(tool_on(connection, action, resource), "a")
    assert e.code == "authorization_error"
    assert "may not" not in e.reason
    assert "try again" in e.reason


@pytest.mark.parametrize(("connection", "action"), [("github-acme", "pr.create"), ("things", "thing.nope")])
def test_a_missing_connection_or_action_is_not_configured(dana, connection, action):
    e = refused(tool_on(connection, action), "a")
    assert e.code == "not_configured"
    assert "may not" not in e.reason
    assert "nothing was checked" in e.reason and "operator" in e.reason


def test_an_unreachable_server_is_authorization_error(dana):
    remote = Toolpass.remote("https://127.0.0.1:1", "key")
    e = refused(tool_on("things", "thing.write", tp=remote), "a")
    assert e.code == "authorization_error"
    assert "may not" not in e.reason


def test_an_allow_still_runs(dana):
    assert tool_on("things", "thing.read")("a") == "ran"


def test_a_checker_answering_something_odd_is_not_a_deny(dana):
    class Odd:
        def check(self, *a, **kw):
            return Decision("maybe", "weird: who knows")

    e = refused(tool_on("things", "thing.write", tp=Odd()), "a")
    assert e.code == "authorization_error"


def test_an_authorizer_can_name_the_refusal(dana):
    tools = Toolkit(audit=None)

    @tools.tool(effect="write", authorize=lambda call: AuthDecision(False, "groot is down", "authorization_error"))
    def a() -> str:
        return "ran"

    @tools.tool(effect="write", authorize=lambda call: AuthDecision(False, "no policy", "not_configured"))
    def b() -> str:
        return "ran"

    @tools.tool(effect="write", authorize=lambda call: AuthDecision(False, "nope", "something_else"))
    def c() -> str:
        return "ran"

    assert refused(a).code == "authorization_error"
    assert refused(b).code == "not_configured"
    assert refused(c).code == "not_authorized"  # an unknown code keeps the old meaning


def test_entitlements_without_a_permissions_engine_are_not_configured(dana):
    ents = {"tools": {"write": {"actions": [{"action": "fake:thing.write", "resource": "thing:{key}", "as": "user", "effect": "write"}]}}}
    tools = Toolkit(audit=None, entitlements=ents)

    @tools.tool()
    def write(key: str) -> str:
        return "ran"

    e = refused(write, "a")
    assert e.code == "not_configured"
    assert "nothing was checked" in e.reason

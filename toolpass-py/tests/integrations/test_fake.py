"""Port of v0.5.0:internal/integrations/fake/fake_test.go."""

from __future__ import annotations

import pytest

from toolpass.core import catalog
from toolpass.core.context import background
from toolpass.core.decision import Code, Decision, Outcome, to_decision
from toolpass.core.integration import CheckRequest, Connection, Deps, Settings, User, find_action
from toolpass.core.log import discard
from toolpass.integrations.fake import Fake


def _no_deps() -> Deps:
    """Go's zero integration.Deps: the fake uses none of it."""

    def no_connection(id: str) -> Connection:
        raise AssertionError("the fake needs no other connection")

    def no_http(s: Settings) -> object:
        raise AssertionError("the fake makes no HTTP calls")

    return Deps(logger=discard(), connection=no_connection, http_client=no_http)  # type: ignore[arg-type]


def conn(fail: str) -> Connection:
    s = Settings("f", "fake", {"users": "u@x.com, U2@x.com", "admins": "a@x.com", "fail": fail})
    return Fake().new(background(), s, _no_deps())


def check(c: Connection, email: str, action: str, resource: str) -> Decision:
    ctx = background()
    try:
        ident = c.resolve_identity(ctx, User(email=email))
    except Exception as e:
        return to_decision(e)
    res = catalog.parse_resource(resource)
    act = find_action(Fake(), action)
    assert act is not None
    try:
        return c.check(ctx, CheckRequest(user=User(email=email), identity=ident, action=act, action_name=action, resource=res))
    except Exception as e:
        return to_decision(e)


def test_action_thing_read_allow() -> None:
    d = check(conn("none"), "u2@x.com", "thing.read", "thing:1")
    assert d.outcome == Outcome.ALLOW, d


def test_action_thing_read_deny() -> None:
    d = check(conn("none"), "nobody@x.com", "thing.read", "thing:1")
    assert d.code == Code.USER_NOT_FOUND and d.outcome == Outcome.DENY, d


def test_action_thing_write_allow() -> None:
    d = check(conn("none"), "a@x.com", "thing.write", "thing:1")
    assert d.outcome == Outcome.ALLOW, d


def test_action_thing_write_deny() -> None:
    d = check(conn("none"), "u@x.com", "thing.write", "thing:1")
    assert d.code == Code.DENIED, d


def test_action_thing_admin_allow() -> None:
    d = check(conn("none"), "a@x.com", "thing.admin", "thing:1")
    assert d.outcome == Outcome.ALLOW, d


def test_action_thing_admin_deny() -> None:
    d = check(conn("none"), "u@x.com", "thing.admin", "thing:1")
    assert d.code == Code.DENIED, d


def test_unknowns() -> None:
    c = conn("none")
    d = check(c, "ambiguous@x.com", "thing.read", "thing:1")
    assert d.code == Code.USER_AMBIGUOUS, d
    d = check(c, "u@x.com", "thing.read", "thing:hidden")
    assert d.code == Code.RESOURCE_NOT_VISIBLE, d
    d = check(c, "u@x.com", "thing.read", "thing:broken")
    assert d.code == Code.UNSUPPORTED, d
    d = check(c, "u@x.com", "thing.read", "other:1")
    assert d.code == Code.INVALID_REQUEST, d
    d = check(conn("upstream_timeout"), "u@x.com", "thing.read", "thing:1")
    assert d.code == Code.UPSTREAM_TIMEOUT, d
    with pytest.raises(Exception):  # noqa: B017 - Go: any error
        conn("credential_rejected").probe(background())
    r = c.probe(background())
    assert r.summary != "", r


def test_fake_groups_source() -> None:
    from toolpass.core.decision import ToolpassError
    from toolpass.integrations.fake import FakeConnection, _validate_groups

    deps = None  # Fake.new reads no deps
    s = Settings("dir", "fake", {"users": "bob@example.com", "groups": "Dana@Example.com=platform-team, ops ;eve@example.com=", "groups_mapped": "false"})
    c = Fake().new(background(), s, deps)  # type: ignore[arg-type]
    assert isinstance(c, FakeConnection) and c.kubernetes_needs_cluster is False
    assert c.user_groups(background(), "dana@example.com") == ("platform-team", "ops")
    sub = c.kubernetes_subject(background(), User(email="dana@example.com"), "")
    assert sub.groups == ("platform-team", "ops") and not sub.mapped and sub.found
    assert c.kubernetes_subject(background(), User(email="eve@example.com"), "").groups == ()
    assert c.kubernetes_subject(background(), User(email="bob@example.com"), "").groups == ()
    with pytest.raises(ToolpassError) as ei:
        c.kubernetes_subject(background(), User(email="nobody@example.com"), "")
    assert ei.value.code == Code.USER_NOT_FOUND
    mapped = Fake().new(background(), Settings("dir", "fake", {"groups": "ops@example.com=system:masters", "groups_mapped": "true"}), deps)  # type: ignore[arg-type]
    assert isinstance(mapped, FakeConnection) and mapped.kubernetes_subject(background(), User(email="ops@example.com"), "").mapped
    for bad in ("dana", "=x", "dana@example.com=a;dana@example.com=b"):
        with pytest.raises(ValueError):
            _validate_groups(bad)
    failing = Fake().new(background(), Settings("dir", "fake", {"fail": "upstream_error"}), deps)  # type: ignore[arg-type]
    with pytest.raises(ToolpassError) as ei:
        failing.kubernetes_subject(background(), User(email="dana@example.com"), "")  # type: ignore[attr-defined]
    assert ei.value.code == Code.UPSTREAM_ERROR

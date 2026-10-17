"""The okta integration against a fake Okta org. No Okta API description is
fetched by test/specs/fetch.sh, so these requests are not validated
against one; the shapes follow Okta's Management API reference (users,
users/{id}/groups with Link pagination)."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from typing import Any

import pytest

from tests import harness as itest
from toolpass.core.catalog import parse_resource
from toolpass.core.context import background
from toolpass.core.decision import Code, Decision, ToolpassError
from toolpass.core.integration import Connection, User, find_action
from toolpass.core.routes import Request
from toolpass.integrations.okta import INTEGRATION, Okta, OktaConnection
from toolpass.integrations.okta.routes import ROUTES


class FakeOkta:
    def __init__(self, srv: itest.Server) -> None:
        self.srv = srv
        self.users: dict[str, dict[str, Any]] = {
            "dana@example.com": {"id": "00u1", "status": "ACTIVE", "profile": {"login": "dana@example.com"}},
            "bob@example.com": {"id": "00u2", "status": "PASSWORD_EXPIRED", "profile": {"login": "bob@example.com"}},
            "off@example.com": {"id": "00u3", "status": "SUSPENDED", "profile": {"login": "off@example.com"}},
            "gone@example.com": {"id": "00u4", "status": "DEPROVISIONED", "profile": {"login": "gone@example.com"}},
            "new@example.com": {"id": "00u6", "status": "STAGED", "profile": {"login": "new@example.com"}},
            # Okta matches a login shortname: this lookup returns another user.
            "short@example.com": {"id": "00u5", "status": "ACTIVE", "profile": {"login": "short@other.example"}},
        }
        self.groups: dict[str, list[tuple[str, str]]] = {
            "00u1": [("00gA", "Everyone"), ("00gB", "platform-team"), ("00gC", "viewers")],
            "00u2": [("00gC", "viewers")],
        }
        self.per_page = 2
        self.next_host = ""  # a next link pointing elsewhere
        self.errors: list[str] = []

    def handler(self, w: itest.ResponseWriter, r: itest.Request) -> None:
        if r.header.get("Authorization") != "SSWS " + itest.CANARY + "okta":
            w.write_header(401)
            w.write('{"errorCode":"E0000011","errorSummary":"Invalid token provided"}')
            return
        w.header().set("Content-Type", "application/json")
        parts = r.path.split("/")
        if r.path == "/api/v1/users":
            w.write(json.dumps([{"id": "00u1"}][: int(r.q("limit") or "1")]))
            return
        if len(parts) == 5 and parts[3] == "users":
            u = self.users.get(parts[4])
            if u is None:
                w.write_header(404)
                w.write('{"errorCode":"E0000007","errorSummary":"Not found"}')
                return
            w.write(json.dumps(u))
            return
        if len(parts) == 6 and parts[3] == "users" and parts[5] == "groups":
            if r.q("limit") != "200":
                self.errors.append(f"groups limit {r.query}")
            gs = self.groups.get(parts[4], [])
            start = int(r.q("after") or "0")
            page = gs[start : start + self.per_page]
            if start + self.per_page < len(gs):
                base = self.next_host or self.srv.url
                w.header().set("Link", f'<{base}/api/v1/users/{parts[4]}/groups?after={start + self.per_page}&limit=200>; rel="next"')
            w.write(json.dumps([{"id": gid, "type": "OKTA_GROUP", "profile": {"name": name}} for gid, name in page]))
            return
        w.write_header(404)


Setup = Callable[[], "tuple[itest.Server, FakeOkta, Connection]"]


@pytest.fixture
def setup() -> Iterator[Setup]:
    servers: list[itest.Server] = []
    fakes: list[FakeOkta] = []

    def make() -> tuple[itest.Server, FakeOkta, Connection]:
        srv = itest.Server()
        servers.append(srv)
        f = FakeOkta(srv)
        fakes.append(f)
        srv.handle("", "/api/*", f.handler)
        deps, _ = itest.deps(srv)
        s = itest.settings("okta", "okta", {"url": srv.url}, {"credential": itest.literal("okta")})
        return srv, f, Okta().new(background(), s, deps)

    yield make
    for s in servers:
        s.close()
    for f in fakes:
        assert not f.errors, f.errors


dana = User(email="dana@example.com")
bob = User(email="bob@example.com")


def check(c: Connection, u: User, action: str, resource: str) -> Decision:
    return itest.check(c, Okta(), u, action, resource)


def test_action_group_member_allow(setup: Setup) -> None:
    _, _, c = setup()
    # By name on the second page, and by id.
    itest.expect_code(check(c, dana, "group.member", "group:viewers"), Code.ALLOWED)
    itest.expect_code(check(c, dana, "group.member", "group:00gB"), Code.ALLOWED)
    itest.expect_code(check(c, bob, "group.member", "group:viewers"), Code.ALLOWED)


def test_action_group_member_deny(setup: Setup) -> None:
    _, _, c = setup()
    itest.expect_code(check(c, bob, "group.member", "group:platform-team"), Code.DENIED)
    itest.expect_code(check(c, User(email="off@example.com"), "group.member", "group:viewers"), Code.DENIED)
    itest.expect_code(check(c, User(email="nobody@example.com"), "group.member", "group:viewers"), Code.USER_NOT_FOUND)


def test_resolve_identity(setup: Setup) -> None:
    _, _, c = setup()
    ident = c.resolve_identity(background(), dana)
    assert ident.id == "00u1" and ident.display == "dana@example.com" and ident.attr("status") == "ACTIVE"
    for email, code in (("nobody@example.com", Code.USER_NOT_FOUND), ("short@example.com", Code.USER_NOT_FOUND), ("not an email", Code.INVALID_REQUEST)):
        with pytest.raises(ToolpassError) as ei:
            c.resolve_identity(background(), User(email=email))
        assert ei.value.code == code, (email, ei.value)


def test_kubernetes_subject(setup: Setup) -> None:
    _, _, c = setup()
    assert isinstance(c, OktaConnection)
    s = c.kubernetes_subject(background(), "dana@example.com", "")
    assert s.groups == ("Everyone", "platform-team", "viewers") and not s.mapped and s.username == "", s
    assert c.kubernetes_subject(background(), "bob@example.com", "").groups == ("viewers",)
    for email in ("off@example.com", "gone@example.com", "new@example.com", "nobody@example.com"):
        with pytest.raises(ToolpassError) as ei:
            c.kubernetes_subject(background(), email, "")
        assert ei.value.code == Code.USER_NOT_FOUND, (email, ei.value)


def test_next_link_must_stay_on_the_org(setup: Setup) -> None:
    _, f, c = setup()
    f.next_host = "https://evil.example.com"
    d = check(c, dana, "group.member", "group:viewers")
    itest.expect_code(d, Code.UPSTREAM_ERROR)


def test_bad_token_and_resource(setup: Setup) -> None:
    srv = itest.Server()
    try:
        f = FakeOkta(srv)
        srv.handle("", "/api/*", f.handler)
        deps, _ = itest.deps(srv)
        s = itest.settings("okta", "okta", {"url": srv.url}, {"credential": itest.literal("wrong")})
        c = Okta().new(background(), s, deps)
        itest.expect_code(check(c, dana, "group.member", "group:viewers"), Code.CREDENTIAL_REJECTED)
        with pytest.raises(ToolpassError) as ei:
            c.probe(background())
        assert ei.value.code == Code.CREDENTIAL_REJECTED
    finally:
        srv.close()
    _, _, c = setup()
    itest.expect_code(check(c, dana, "group.member", "team:viewers"), Code.INVALID_REQUEST)


def test_probe(setup: Setup) -> None:
    _, _, c = setup()
    r = c.probe(background())
    assert "reads users" in r.summary and any("Read-only Administrator" in w for w in r.warnings), r


def test_new_requires_credential() -> None:
    deps, _ = itest.deps()
    with pytest.raises(ValueError, match="credential"):
        Okta().new(background(), itest.settings("okta", "okta", {"url": "https://acme.okta.com"}), deps)


def test_routes() -> None:
    def obs(method: str, path: str) -> Any:
        return ROUTES.match(Request("https", "acme.okta.com", 443, method, path, "", {}, b""))

    o = obs("GET", "/api/v1/groups/00gB/users")
    assert o.action == "group.member" and o.resource == "group:00gB" and o.effect == "read"
    assert obs("GET", "/api/v1/users/me").action is None
    assert obs("PUT", "/api/v1/groups/00gB/users/00u1") is None and ROUTES.is_unrouted("PUT", "/api/v1/groups/00gB/users/00u1")
    # Reads are not "left out": an unrouted read passes, untrusted.
    assert ROUTES.is_unrouted("GET", "/api/v1/users/00u1") is None
    for r in ROUTES.routes:
        if r.action is not None:
            assert find_action(INTEGRATION, r.action) is not None, r
            parse_resource(r.resource.replace("{groupId}", "00gB"))
    assert ROUTES.endpoints(itest.settings("okta", "okta", {"url": "https://acme.okta.com"})) == [("acme.okta.com", "")]

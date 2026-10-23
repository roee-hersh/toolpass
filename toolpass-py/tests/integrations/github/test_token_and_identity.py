"""Token auth instead of an App, and the http and callable identity modes.

Each test tries to get a deny or an allow out of something that should be
unknown: a token that cannot read SAML, an identity service that fails or
answers garbage, a callable that raises."""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Iterator
from typing import Any

import pytest

from tests import harness as itest
from tests.integrations.github.test_github import BOB, DANA, Clock, Env, Fake, Harness, expect_text, write_json
from toolpass.core.context import background
from toolpass.core.decision import Code
from toolpass.core.integration import User
from toolpass.core.secret import Secret
from toolpass.core.secret import file as secret_file
from toolpass.integrations.github import GitHub

TOKEN = itest.CANARY + "ghp_reader"


@pytest.fixture
def gh() -> Iterator[Harness]:
    h = Harness()
    yield h
    h.close()


def token_env(gh: Harness, values: dict[str, str] | None = None, secrets: dict[str, Secret] | None = None) -> Env:
    """A connection on token auth against the fake, which accepts TOKEN."""
    srv = gh.server()
    api = Fake()
    api.accepted = TOKEN
    gh.fakes.append(api)
    srv.handle("", "/api/*", api.handle)
    ck = Clock(time.time())
    deps, logs = itest.deps(srv, now=ck.now)
    v = {"url": srv.url, "organization": "acme", "identity_mode": "saml"}
    v.update(values or {})
    sec: dict[str, Secret] = {"token": itest.literal("ghp_reader")}
    sec.update(secrets or {})
    c = GitHub().new(background(), itest.settings("gh", "github", v, sec), deps)
    return Env(srv=srv, api=api, conn=c, clock=ck, logs=logs)


# -- token auth ----------------------------------------------------------------


def test_token_reads_with_the_same_decisions_as_the_app(gh: Harness) -> None:
    e = token_env(gh)
    itest.expect_code(e.check(DANA, "repo.push", "repo:acme/api"), Code.ALLOWED)
    itest.expect_code(e.check(BOB, "repo.push", "repo:acme/api"), Code.DENIED)
    itest.expect_code(e.check(BOB, "pr.create", "repo:acme/webapp"), Code.DENIED)
    itest.expect_code(e.check(DANA, "team.maintainer", "team:acme/platform"), Code.ALLOWED)
    paths = [c.path for c in e.srv.calls()]
    assert not any("installation" in p or p == "/api/v3/app" for p in paths), f"token auth touched App endpoints: {paths}"
    assert all(c.header.get("Authorization") == "Bearer " + TOKEN for c in e.srv.calls())


def test_a_rejected_token_is_not_retried_and_not_a_deny(gh: Harness) -> None:
    e = token_env(gh, {"identity_mode": "template", "email_domains": "example.com"})
    e.api.with_(lambda: setattr(e.api, "accepted", "something-else"))
    d = e.check(DANA, "repo.push", "repo:acme/api")
    expect_text(d, Code.CREDENTIAL_REJECTED, "the token was rejected")
    assert e.calls("/users/dana") == 1, "a 401 on a static token was retried"


def test_a_token_that_cannot_read_saml_is_not_configured_never_deny(gh: Harness) -> None:
    # No identity settings: the connection falls back to SAML, which this token cannot read.
    # Nothing about the user was checked, so the answer says the deployment is unconfigured.
    e = token_env(gh)
    e.api.with_(lambda: setattr(e.api, "gql_errors", [{"type": "FORBIDDEN", "message": "owner only"}]))
    expect_text(e.check(DANA, "repo.push", "repo:acme/api"), Code.NOT_CONFIGURED, "SAML identities")
    e.api.with_(lambda: setattr(e.api, "gql_errors", [{"type": "INSUFFICIENT_SCOPES", "message": "admin:org"}]))
    expect_text(e.check(DANA, "repo.push", "repo:acme/api"), Code.NOT_CONFIGURED, "configure identity_url")
    e.api.with_(lambda: (setattr(e.api, "gql_errors", None), setattr(e.api, "saml", False)))
    expect_text(e.check(DANA, "repo.push", "repo:acme/api"), Code.NOT_CONFIGURED, "no SAML identity provider")
    assert e.calls("/permission") == 0


def test_not_configured_reaches_a_tool_as_not_configured(gh: Harness) -> None:
    from toolpass import Session, Toolkit, ToolRefused, permission_check
    from toolpass._api import Decision as ApiDecision

    e = token_env(gh)
    e.api.with_(lambda: setattr(e.api, "gql_errors", [{"type": "FORBIDDEN", "message": "owner only"}]))

    class Engine:
        def check(self, user: str, connection: str, action: str, resource: str, groups: Any = None, *, fresh: bool = False) -> ApiDecision:
            d = e.check(User(email=user), action, resource)
            return ApiDecision(d.outcome.value, d.reason())

    tools = Toolkit(audit=None)

    @tools.tool(effect="write", authorize=permission_check(Engine(), "gh", "repo.push", "repo:{repo}"))
    def push(repo: str) -> str:
        return "pushed"

    with Session("dana@example.com").active(), pytest.raises(ToolRefused) as r:
        push("acme/api")
    assert r.value.code == "not_configured" and r.value.reason.startswith("nothing was checked")
    assert "may not" not in r.value.reason


@pytest.mark.parametrize("deny", [False, True])
def test_collaborator_404_is_unknown_unless_configured_as_deny(gh: Harness, deny: bool) -> None:
    v = {"identity_mode": "template", "email_domains": "example.com"}
    if deny:
        v["collaborator_404"] = "deny"
    e = token_env(gh, v)
    d = e.check(User(email="frank@example.com"), "repo.push", "repo:acme/api")  # frank has no record on api
    if deny:
        expect_text(d, Code.DENIED, "not a collaborator")
    else:
        itest.expect_code(d, Code.RESOURCE_NOT_VISIBLE)
    # A repository toolpass cannot see says nothing about the login: unknown either way.
    e.api.with_(lambda: setattr(e.api, "repo_status", 404))
    itest.expect_code(e.check(User(email="frank@example.com"), "repo.push", "repo:acme/api"), Code.RESOURCE_NOT_VISIBLE)
    e.api.with_(lambda: setattr(e.api, "repo_status", 0))
    # Other failures of the same read stay unknown either way.
    e.api.with_(lambda: setattr(e.api, "perm_status", 500))
    itest.expect_code(e.check(DANA, "repo.push", "repo:acme/api"), Code.UPSTREAM_ERROR)


def test_a_token_that_cannot_read_branch_rules_is_unsupported_not_deny(gh: Harness) -> None:
    e = token_env(gh, {"identity_mode": "template", "email_domains": "example.com"})
    e.api.with_(lambda: setattr(e.api, "rules_status", 403))
    expect_text(e.check(DANA, "repo.push", "repo:acme/api@main"), Code.UNSUPPORTED, "the token")
    e.api.with_(lambda: (setattr(e.api, "rules_status", 0), setattr(e.api, "protection_status", 403)))
    expect_text(e.check(DANA, "repo.push", "repo:acme/api@release"), Code.UNSUPPORTED, "branch protection")


def test_token_and_app_settings_do_not_mix(gh: Harness) -> None:
    deps, _ = itest.deps()
    base = {"organization": "acme", "identity_mode": "template", "email_domains": "example.com"}
    cases: list[tuple[dict[str, str], dict[str, Secret], str]] = [
        ({}, {}, "or token is required"),
        ({"app_id": "Iv1.x"}, {"credential": itest.literal("k"), "token": itest.literal("t")}, "not both"),
        ({"app_id": "Iv1.x"}, {"token": itest.literal("t")}, "apply to App auth"),
        ({"installation_id": "42"}, {"token": itest.literal("t")}, "apply to App auth"),
        ({}, {"credential": itest.literal("k")}, "app_id is required"),
    ]
    for values, secrets, want in cases:
        with pytest.raises(ValueError, match=want):
            GitHub().new(background(), itest.settings("gh", "github", {**base, **values}, secrets), deps)


def test_probe_with_a_token_reports_write_scopes(gh: Harness) -> None:
    e = token_env(gh, {"identity_mode": "template", "email_domains": "example.com"})

    def org(w: itest.ResponseWriter, r: itest.Request) -> None:
        w.header().set("X-OAuth-Scopes", "repo, read:org, admin:org")
        write_json(w, 200, {"login": "acme"})

    e.srv.handle("GET", "/api/v3/orgs/acme", org)
    p = e.conn.probe(background())
    assert p.summary == "token reads organization acme (scopes admin:org, read:org, repo)"
    assert len(p.warnings) == 2
    assert p.warnings[0] == "over-privileged: the token has scope admin:org; toolpass only reads"
    assert p.warnings[1].startswith("over-privileged: the token has scope repo, which classic tokens need")


def test_the_token_is_read_at_every_request(gh: Harness, tmp_path: Any) -> None:
    f = tmp_path / "token"
    f.write_text("wrong")
    e = token_env(gh, {"identity_mode": "template", "email_domains": "example.com"}, {"token": secret_file(str(f))})
    itest.expect_code(e.check(DANA, "repo.read", "repo:acme/api"), Code.CREDENTIAL_REJECTED)
    f.write_text(TOKEN)  # rotated in place
    itest.expect_code(e.check(DANA, "repo.read", "repo:acme/api"), Code.ALLOWED)


# -- identity_mode http --------------------------------------------------------


class Groot:
    """An identity service: GET /v1/github-login?email= answers the login as a JSON string."""

    def __init__(self) -> None:
        self.logins = {"dana@example.com": "dana", "bob@example.com": "bob"}
        self.status = 0  # override (0: normal)
        self.body: bytes | None = None  # override
        self.asked: list[str] = []
        self.auth: list[str] = []

    def handle(self, w: itest.ResponseWriter, r: itest.Request) -> None:
        email = r.q("email")
        self.asked.append(email)
        self.auth.append(r.header.get("Authorization"))
        if self.status:
            w.write_header(self.status)
            w.write(self.body or b"{}")
            return
        if self.body is not None:
            w.write_header(200)
            w.write(self.body)
            return
        if "@" not in email:
            write_json(w, 422, {"error": "not an address"})
            return
        login = self.logins.get(email)
        if login is None:
            write_json(w, 404, {"error": "no identity"})
            return
        write_json(w, 200, login)


def http_env(gh: Harness, groot: Groot, **kw: Any) -> Env:
    ids = gh.server(spec=False)
    ids.handle("GET", "/v1/github-login", groot.handle)
    sec = {"identity_token": itest.literal("groot")} if kw.pop("bearer", True) else {}
    return token_env(gh, {"identity_mode": "http", "identity_url": ids.url + "/v1/github-login", **kw}, sec)


def test_http_identity_resolves_and_checks(gh: Harness) -> None:
    groot = Groot()
    e = http_env(gh, groot)
    itest.expect_code(e.check(DANA, "repo.push", "repo:acme/api"), Code.ALLOWED)
    itest.expect_code(e.check(BOB, "repo.push", "repo:acme/api"), Code.DENIED)
    assert groot.asked == ["dana@example.com", "bob@example.com"]
    assert groot.auth == ["Bearer " + itest.CANARY + "groot"] * 2
    assert e.calls("/api/graphql") == 0, "http mode read SAML identities"


def test_http_identity_url_alone_selects_http_mode() -> None:
    deps, _ = itest.deps()
    v = {"organization": "acme", "identity_url": "https://groot.example/v1/github-login"}
    c = GitHub().new(background(), itest.settings("gh", "github", v, {"token": itest.literal("t")}), deps)
    assert c.mode == "http"  # type: ignore[attr-defined]


def test_http_identity_without_a_bearer_sends_no_authorization(gh: Harness) -> None:
    groot = Groot()
    e = http_env(gh, groot, bearer=False)
    itest.expect_code(e.check(DANA, "repo.read", "repo:acme/api"), Code.ALLOWED)
    assert groot.auth == [""]


def test_http_no_identity_and_refused_address_deny(gh: Harness) -> None:
    groot = Groot()
    e = http_env(gh, groot)
    expect_text(e.check(User(email="nobody@example.com"), "repo.read", "repo:acme/api"), Code.USER_NOT_FOUND, "HTTP 404")
    groot.status, groot.body = 422, b'{"error":"refused"}'
    expect_text(e.check(DANA, "repo.read", "repo:acme/api"), Code.USER_NOT_FOUND, "HTTP 422")


@pytest.mark.parametrize(
    ("status", "body", "why"),
    [
        (500, b"{}", "HTTP 500"),
        (503, b"{}", "HTTP 503"),
        (401, b"{}", "HTTP 401"),
        (403, b"{}", "HTTP 403"),
        (302, b"", "HTTP 302"),  # a redirect is not followed, and not an answer
        (0, b"dana", "not JSON"),
        (0, b"", "not JSON"),
        (0, b'{"login": "dana"}', "not a login string"),
        (0, b'["dana"]', "not a login string"),
        (0, b"null", "not a login string"),
        (0, b'"dana/../admin"', "not a GitHub login"),
        (0, b'"acme-"', "not a GitHub login"),
        (0, b'"dana\\n"', "not a GitHub login"),
        (0, b'"' + b"a" * 200 + b'"', "not a GitHub login"),
        (0, b"\xff\xfe", "not JSON"),
    ],
)
def test_http_failures_are_unsupported_never_deny(gh: Harness, status: int, body: bytes, why: str) -> None:
    groot = Groot()
    e = http_env(gh, groot)
    groot.status, groot.body = status, body
    d = e.check(DANA, "repo.read", "repo:acme/api")
    expect_text(d, Code.UNSUPPORTED, why)
    assert e.calls("/permission") == 0, "a failed identity lookup went on to the permission read"


def test_http_identity_service_unreachable_is_unsupported(gh: Harness) -> None:
    groot = Groot()
    ids = gh.server(spec=False)
    url = ids.url + "/v1/github-login"
    ids.close()
    e = token_env(gh, {"identity_mode": "http", "identity_url": url})
    expect_text(e.check(DANA, "repo.read", "repo:acme/api"), Code.UNSUPPORTED, "could not be asked")
    assert groot.asked == []


def test_http_email_cannot_change_the_query(gh: Harness) -> None:
    groot = Groot()
    e = http_env(gh, groot)
    e.check(User(email="x&email=dana@example.com"), "repo.read", "repo:acme/api")
    e.check(User(email="eve@example.com#frag"), "repo.read", "repo:acme/api")
    assert groot.asked == ["x&email=dana@example.com", "eve@example.com#frag"], groot.asked


def test_http_mode_settings(gh: Harness) -> None:
    deps, _ = itest.deps()
    base = {"organization": "acme"}
    tok = {"token": itest.literal("t")}
    cases: list[tuple[dict[str, str], dict[str, Secret], str]] = [
        ({"identity_mode": "http"}, tok, "requires identity_url"),
        ({"identity_mode": "saml", "identity_url": "https://groot.example/v1"}, tok, "applies to identity_mode http"),
        ({"identity_mode": "template", "email_domains": "example.com"}, {**tok, "identity_token": itest.literal("g")}, "applies to identity_mode http"),
    ]
    for values, secrets, want in cases:
        with pytest.raises(ValueError, match=want):
            GitHub().new(background(), itest.settings("gh", "github", {**base, **values}, secrets), deps)
    f = next(f for f in GitHub().fields() if f.name == "identity_url")
    assert f.validate is not None
    for bad in ("ftp://groot.example/v1", "https://groot.example/v1?email=x", "https://user@groot.example/v1", "http://u@groot/v1", "http://groot/v1#x"):
        with pytest.raises(ValueError):
            f.validate(bad)
    f.validate("http://groot-service.security.svc.cluster.local/v1")  # the shape is fine; whether http is allowed is new()'s


def test_plain_http_identity_url_needs_the_opt_in(gh: Harness) -> None:
    deps, _ = itest.deps()
    v = {"organization": "acme", "identity_url": "http://groot-service.security.svc.cluster.local/v1/github-login"}
    tok = {"token": itest.literal("t")}
    with pytest.raises(ValueError, match="identity_allow_http"):
        GitHub().new(background(), itest.settings("gh", "github", v, tok), deps)
    with pytest.raises(ValueError, match="identity_allow_http applies to identity_mode http"):
        sv = {"organization": "acme", "identity_mode": "saml", "identity_allow_http": "true"}
        GitHub().new(background(), itest.settings("gh", "github", sv, tok), deps)
    f = next(f for f in GitHub().fields() if f.name == "identity_url")
    assert f.validate is not None
    with pytest.raises(ValueError, match=re.escape('"http://groot.svc/v1?x=1"')):  # the URL as written, not as rewritten
        f.validate("http://groot.svc/v1?x=1")
    c = GitHub().new(background(), itest.settings("gh", "github", {**v, "identity_allow_http": "true"}, tok), deps)
    assert c.identity_url == v["identity_url"]  # type: ignore[attr-defined]
    # Loopback needs no opt-in, as everywhere else.
    GitHub().new(background(), itest.settings("gh", "github", {**v, "identity_url": "http://127.0.0.1:8080/v1"}, tok), deps)


def test_plain_http_identity_service_answers(gh: Harness) -> None:
    groot = Groot()
    ids = itest.Server(tls=False)
    gh.servers.append(ids)
    ids.handle("GET", "/v1/github-login", groot.handle)
    e = token_env(gh, {"identity_mode": "http", "identity_url": ids.url + "/v1/github-login", "identity_allow_http": "true"})
    itest.expect_code(e.check(DANA, "repo.push", "repo:acme/api"), Code.ALLOWED)
    assert groot.asked == ["dana@example.com"]


def test_the_decision_carries_the_resolved_login(gh: Harness) -> None:
    from toolpass import Toolpass, literal

    e = token_env(gh, {"identity_mode": "template", "email_domains": "example.com"})
    logins = {"dana@example.com": "dana", "bob@example.com": "bob"}
    conn = {"id": "gh", "integration": "github", "url": e.srv.url, "ca_file": itest.test_ca().ca_file, "organization": "acme"}
    tp = Toolpass(connections=[{**conn, "token": literal(TOKEN), "identity": logins.get}])
    d = tp.check("dana@example.com", "gh", "repo.push", "repo:acme/api")
    assert (d.decision, d.identity) == ("allow", "dana"), d
    d = tp.check("bob@example.com", "gh", "repo.push", "repo:acme/api")
    assert (d.decision, d.identity) == ("deny", "bob"), d
    d = tp.check("bob@example.com", "gh", "repo.push", "repo:acme/api")  # from the decision cache
    assert (d.decision, d.identity) == ("deny", "bob"), d
    d = tp.check("nobody@example.com", "gh", "repo.push", "repo:acme/api")
    assert (d.decision, d.code, d.identity) == ("deny", "user_not_found", ""), d


# -- identity_mode callable ----------------------------------------------------


def callable_env(gh: Harness, fn: Any) -> Env:
    e = token_env(gh, {"identity_mode": "template", "email_domains": "example.com"})
    deps, _ = itest.deps(e.srv)
    s = itest.settings("gh", "github", {"url": e.srv.url, "organization": "acme"}, {"token": itest.literal("ghp_reader")}, objects={"identity": fn})
    e.conn = GitHub().new(background(), s, deps)
    return e


def test_callable_identity(gh: Harness) -> None:
    e = callable_env(gh, {"dana@example.com": "dana"}.get)
    assert e.conn.mode == "callable"  # type: ignore[attr-defined]
    itest.expect_code(e.check(DANA, "repo.push", "repo:acme/api"), Code.ALLOWED)
    expect_text(e.check(BOB, "repo.push", "repo:acme/api"), Code.USER_NOT_FOUND, "identity callable")


def test_async_callable_identity_inside_and_outside_an_event_loop(gh: Harness) -> None:
    async def lookup(email: str) -> str | None:
        await asyncio.sleep(0)
        return "dana" if email == "dana@example.com" else None

    e = callable_env(gh, lookup)
    itest.expect_code(e.check(DANA, "repo.push", "repo:acme/api"), Code.ALLOWED)

    async def from_a_loop() -> Any:
        return e.check(DANA, "repo.push", "repo:acme/api")

    itest.expect_code(asyncio.run(from_a_loop()), Code.ALLOWED)
    itest.expect_code(e.check(BOB, "repo.push", "repo:acme/api"), Code.USER_NOT_FOUND)


@pytest.mark.parametrize(
    "fn",
    [
        lambda email: (_ for _ in ()).throw(RuntimeError("groot down " + itest.CANARY + "x")),
        lambda email: 42,
        lambda email: "dana/../admin",
        lambda email: "",
        lambda email: {"login": "dana"},
        lambda email: ["dana"],
    ],
)
def test_callable_failures_are_unsupported_never_deny(gh: Harness, fn: Any) -> None:
    e = callable_env(gh, fn)
    d = e.check(DANA, "repo.read", "repo:acme/api")
    itest.expect_code(d, Code.UNSUPPORTED)
    assert itest.CANARY not in d.text


def test_a_slow_async_callable_is_bounded_by_the_deadline(gh: Harness) -> None:
    async def slow(email: str) -> str:
        await asyncio.sleep(30)
        return "dana"

    e = callable_env(gh, slow)
    t = time.monotonic()
    itest.expect_code(e.check(DANA, "repo.read", "repo:acme/api"), Code.UNSUPPORTED)
    assert time.monotonic() - t < 10


def test_identity_is_only_given_in_code() -> None:
    from toolpass.core.config import ConfigErrors, parse
    from toolpass.integrations import registry

    doc = """
connections:
  - id: gh
    integration: github
    organization: acme
    token: env:GH_TOKEN
    identity: mymodule.lookup
"""
    with pytest.raises((ConfigErrors, ValueError), match="given in code"):
        parse("t.yaml", doc, registry(), require_api_key=False)


def test_identity_callable_through_toolpass_connections() -> None:
    from toolpass import Toolpass, literal

    seen: list[str] = []

    def lookup(email: str) -> str | None:
        seen.append(email)
        return None

    tp = Toolpass(connections=[{"id": "gh", "integration": "github", "organization": "acme", "token": literal("t"), "identity": lookup}])
    d = tp.check("dana@example.com", "gh", "repo.read", "repo:acme/api")
    assert d.decision == "deny" and d.code == "user_not_found", d
    assert seen == ["dana@example.com"]
    json.dumps(d.reason)  # the reason stays plain text


def test_http_identity_token_that_cannot_be_read_is_unsupported_and_says_why(gh: Harness, tmp_path: Any) -> None:
    groot = Groot()
    ids = gh.server(spec=False)
    ids.handle("GET", "/v1/github-login", groot.handle)
    v = {"identity_mode": "http", "identity_url": ids.url + "/v1/github-login"}
    e = token_env(gh, v, {"identity_token": secret_file(str(tmp_path / "missing"))})
    expect_text(e.check(DANA, "repo.read", "repo:acme/api"), Code.UNSUPPORTED, "identity_token could not be read")
    assert groot.asked == []


def test_a_config_file_cannot_name_the_in_code_placeholder() -> None:
    from toolpass.core.config import IN_CODE, ConfigErrors, parse
    from toolpass.integrations import registry

    for guess in ("<given in code>", IN_CODE.split(" ")[0] + ">"):
        doc = f'connections:\n  - id: gh\n    integration: github\n    organization: acme\n    token: env:T\n    identity: "{guess}"\n'
        with pytest.raises((ConfigErrors, ValueError), match="given in code"):
            parse("t.yaml", doc, registry(), require_api_key=False)

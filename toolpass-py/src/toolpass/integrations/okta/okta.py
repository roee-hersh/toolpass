"""Checks a user's group membership in one Okta org.

toolpass authenticates with an Okta API token (SSWS), finds the user by
login (the email) and lists the groups the user belongs to. The same list
is what an Okta OIDC app's groups claim carries, so a kubernetes or argocd
connection can use an okta connection as its groups_connection. Nothing is
written.
"""

from __future__ import annotations

import re

from toolpass.core import jsonx
from toolpass.core.catalog import Action, Resource
from toolpass.core.context import Context
from toolpass.core.decision import Code, Decision, ToolpassError, allowed, denied, errorf, user_not_found, wrap_error
from toolpass.core.errors import go_lower, go_quote, go_trim_space
from toolpass.core.integration import (
    CheckRequest,
    Connection,
    Deps,
    Field,
    Identity,
    Integration,
    KubernetesSubject,
    ProbeResult,
    Settings,
    User,
    credential_field,
    url_field,
)
from toolpass.core.routes import RouteTable
from toolpass.core.secret import SecretError
from toolpass.core.template import is_email
from toolpass.net import httpx

__all__ = ["INACTIVE", "PAGE_SIZE", "Okta", "OktaConnection", "parse_group"]

PAGE_SIZE = 200

# User statuses that cannot sign in: such a user is a member of nothing.
# STAGED and PROVISIONED users were never activated (or have not set a
# password yet).
INACTIVE = ("STAGED", "PROVISIONED", "SUSPENDED", "DEPROVISIONED")

# An Okta group name: anything printable, up to 255 characters.
_GROUP_RE = re.compile(r"[^\x00-\x1f\x7f]{1,255}")


def parse_group(r: Resource) -> str:
    """The group of a group:<name-or-id> resource."""
    if r.type != "group" or r.id == "":
        raise errorf(Code.INVALID_REQUEST, "resource must be group:<name or id>")
    if not _GROUP_RE.fullmatch(r.id):
        raise errorf(Code.INVALID_REQUEST, f"group {go_quote(r.id)} is not an Okta group name or id")
    return r.id


def classify(err: BaseException, what: str) -> ToolpassError:
    """An Okta API error; 404 is left to the caller."""
    st = httpx.status(err)
    if st == 401:
        return wrap_error(Code.CREDENTIAL_REJECTED, err, "Okta rejected toolpass's API token")
    if st == 403:
        return wrap_error(Code.CREDENTIAL_REJECTED, err, f"Okta refused to {what}: the API token's admin role may not read it")
    he = httpx.classify(err)
    assert he is not None
    return he


class Okta(Integration):
    """The okta product."""

    def name(self) -> str:
        return "okta"

    def fields(self) -> list[Field]:
        return [
            url_field(True, "the Okta org, e.g. https://acme.okta.com"),
            credential_field(True, "API token of a read-only admin (sent as SSWS), env: or file:"),
        ]

    def actions(self) -> list[Action]:
        return [Action(name="group.member", description="the user is a member of the Okta group (resource group:<name or id>)")]

    def routes(self) -> RouteTable | None:
        from toolpass.integrations.okta.routes import ROUTES

        return ROUTES

    def new(self, ctx: Context, s: Settings, d: Deps) -> Connection:
        """Build a connection. It touches no network."""
        cred = s.secret("credential")
        if cred.is_zero():
            raise ValueError("credential is required")
        base = s.get("url").rstrip("/")
        if base == "":
            raise ValueError("url is required")

        def auth(ctx: Context, r: httpx.PreparedRequest) -> None:
            try:
                t = cred.get_string()
            except SecretError as e:
                raise wrap_error(Code.CREDENTIAL_REJECTED, e, "the API token could not be read") from e
            r.headers.set("Authorization", "SSWS " + go_trim_space(t))

        return OktaConnection(httpx.Client(http=d.http_client(s), base=base, auth=auth, logger=d.logger))


class OktaConnection(Connection):
    """One Okta org."""

    # kubernetes_subject needs no cluster name.
    kubernetes_needs_cluster = False

    def __init__(self, api: httpx.Client) -> None:
        self.api = api

    def resolve_identity(self, ctx: Context, u: User) -> Identity:
        """GET /api/v1/users/{login}: Okta finds a user by id, login or
        login shortname; toolpass sends the email, the usual login."""
        email = go_trim_space(u.email)
        if not is_email(email):
            raise errorf(Code.INVALID_REQUEST, f"user email {go_quote(email)} is not an address")
        try:
            _, v = self.api.get_json(ctx, "/api/v1/users/" + httpx.path_escape(email))
            d = jsonx.obj(v)
            uid, status = jsonx.s(d, "id"), jsonx.s(d, "status")
            login = jsonx.s(jsonx.o(d, "profile"), "login")
        except Exception as e:
            if httpx.status(e) == 404:
                raise user_not_found(f"no Okta user has login {email}") from None
            # A body that does not decode is upstream_error, as any other
            # failure classify does not know.
            raise classify(e, "read users") from e
        if uid == "":
            raise errorf(Code.UPSTREAM_ERROR, "Okta returned a user without an id")
        # Okta finds a user by login shortname too; only the full login (or
        # the email it was looked up by) is the caller.
        if login != "" and go_lower(login) != go_lower(email):
            raise user_not_found(f"no Okta user has login {email} (it matched the login {login})")
        return Identity(id=uid, display=login or email, attrs={"status": status, "login": login})

    def _groups(self, ctx: Context, user_id: str) -> list[tuple[str, str]]:
        """(id, profile.name) of every group the user is in, every page."""
        out: list[tuple[str, str]] = []

        def page(resp: httpx.Response) -> httpx.Request | None:
            try:
                for g in jsonx.arr({"v": resp.json()}, "v"):
                    g = jsonx.obj(g)
                    out.append((jsonx.s(g, "id"), jsonx.s(jsonx.o(g, "profile"), "name")))
            except ValueError as e:
                raise wrap_error(Code.UPSTREAM_ERROR, e, "Okta returned an unreadable group list") from e
            nxt = self.api.next_link(resp.header)
            return httpx.Request(method="GET", path=nxt) if nxt != "" else None

        first = httpx.Request(method="GET", path="/api/v1/users/" + httpx.path_escape(user_id) + "/groups", query={"limit": str(PAGE_SIZE)})
        try:
            self.api.paginate(ctx, first, page)
        except httpx.TooManyPages as e:
            raise wrap_error(Code.UPSTREAM_ERROR, e, "the user's group list has too many pages") from e
        except Exception as e:
            # A next link off the org (ValueError from next_link) is
            # upstream_error too: the token never leaves the org's host.
            raise classify(e, "list the user's groups") from e
        return out

    def check(self, ctx: Context, r: CheckRequest) -> Decision:
        """group.member: the group, by name or id, is in the user's list."""
        if r.action_name != "group.member":
            raise errorf(Code.INVALID_REQUEST, f"unknown action {go_quote(r.action_name)}")
        group = parse_group(r.resource)
        who = r.identity.display
        if r.identity.attr("status") in INACTIVE:
            return denied(f"{who} is {go_lower(r.identity.attr('status'))} in Okta")
        for gid, name in self._groups(ctx, r.identity.id):
            if group in (gid, name):
                return allowed(f"{who} is a member of Okta group {name or gid}")
        return denied(f"{who} is not a member of Okta group {group}")

    def kubernetes_subject(self, ctx: Context, email: str, cluster: str) -> KubernetesSubject:
        """The user's group names, as an Okta OIDC groups claim carries
        them. UNVERIFIED: the claim carries the groups' profile.name, as
        Okta documents for the default groups claim. An app's claim is
        usually filtered (a regex or a prefix in its settings); toolpass
        returns every group, so a binding to a group the claim leaves out
        reads as allowed here and denied by the cluster. The username stays
        the kubernetes connection's username_template."""
        ident = self.resolve_identity(ctx, User(email=email))
        if ident.attr("status") in INACTIVE:
            raise user_not_found(f"{ident.display} is {go_lower(ident.attr('status'))} in Okta")
        names: list[str] = []
        for _, name in self._groups(ctx, ident.id):
            if name != "" and name not in names:
                names.append(name)
        return KubernetesSubject(groups=tuple(names))

    def probe(self, ctx: Context) -> ProbeResult:
        """Read one user to prove the token works and may read users."""
        try:
            _, v = self.api.get_json(ctx, "/api/v1/users", {"limit": "1"})
            n = len(jsonx.arr({"v": v}, "v"))
        except Exception as e:
            raise classify(e, "list users") from e
        warnings: list[str] = []
        if n == 0:
            warnings.append("the token listed no users; check the admin role it was created with")
        warnings.append("an Okta API token acts with its creator's admin role; create it as a Read-only Administrator")
        return ProbeResult(summary=f"API token reads users at {self.api.base}", warnings=tuple(warnings))

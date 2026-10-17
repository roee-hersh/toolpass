"""A test integration. It talks to nothing. It exists so the engine, server
and command can be exercised end to end, and so a fresh deployment can be
smoke-tested before any real connection is added."""

from __future__ import annotations

from toolpass.core.catalog import Action
from toolpass.core.context import Context
from toolpass.core.decision import (
    Code,
    Decision,
    allowed,
    denied,
    errorf,
    unknown_decision,
    unsupported,
    user_ambiguous,
    user_not_found,
)
from toolpass.core.integration import CheckRequest, Connection, Deps, Field, Identity, Integration, KubernetesSubject, ProbeResult, Settings, User


class Fake(Integration):
    def name(self) -> str:
        return "fake"

    def fields(self) -> list[Field]:
        return [
            Field(name="users", description="comma-separated emails that exist and may read"),
            Field(name="admins", description="comma-separated emails allowed every action"),
            Field(
                name="groups",
                validate=_validate_groups,
                description="directory groups for kubernetes groups_connection: email=group,group;email=group",
            ),
            Field(
                name="groups_mapped",
                default="false",
                enum=("true", "false"),
                description="hand the groups to kubernetes as Kubernetes group names (as EKS access entries are)",
            ),
            Field(
                name="fail",
                default="none",
                enum=("none", "upstream_timeout", "upstream_error", "credential_rejected", "upstream_rate_limited"),
                description="make every call fail with this code (for testing callers)",
            ),
        ]

    def actions(self) -> list[Action]:
        return [
            Action("thing.read", "read a thing (any known user)"),
            Action("thing.write", "write a thing (admins only)"),
            Action("thing.admin", "administer a thing (admins only)"),
        ]

    def new(self, ctx: Context, s: Settings, d: Deps) -> Connection:
        fail = s.get("fail")
        c = FakeConnection(_set(s.get("users")), _set(s.get("admins")), Code(fail) if fail not in ("", "none") else None)
        c.groups = _groups(s.get("groups"))
        c.groups_mapped = s.bool("groups_mapped", False)
        return c


def _set(csv: str) -> frozenset[str]:
    return frozenset(p.strip().lower() for p in csv.split(",") if p.strip())


def _groups(v: str) -> dict[str, tuple[str, ...]]:
    """email=group,group;email=group -> {email: groups}."""
    out: dict[str, tuple[str, ...]] = {}
    for part in v.split(";"):
        part = part.strip()
        if part == "":
            continue
        email, sep, groups = part.partition("=")
        email = email.strip().lower()
        if not sep or email == "" or "@" not in email:
            raise ValueError(f"groups entry {part!r} must be email=group,group")
        if email in out:
            raise ValueError(f"groups names {email} twice")
        out[email] = tuple(g.strip() for g in groups.split(",") if g.strip())
    return out


def _validate_groups(v: str) -> None:
    _groups(v)


class FakeConnection(Connection):
    # kubernetes_subject needs no cluster name.
    kubernetes_needs_cluster = False

    def __init__(self, users: frozenset[str], admins: frozenset[str], fail: Code | None) -> None:
        self.users = users
        self.admins = admins
        self.fail = fail
        self.groups: dict[str, tuple[str, ...]] = {}
        self.groups_mapped = False

    def kubernetes_subject(self, ctx: Context, email: str, cluster: str) -> KubernetesSubject:
        """The groups listed for the email; a known user with none listed
        is in no group; anyone else has no account."""
        if self.fail is not None:
            raise errorf(self.fail, "fake failure")
        e = email.strip().lower()
        if e in self.groups:
            return KubernetesSubject(groups=self.groups[e], mapped=self.groups_mapped)
        if e in self.users or e in self.admins:
            return KubernetesSubject(mapped=self.groups_mapped)
        raise user_not_found(f"no account for {email}")

    def resolve_identity(self, ctx: Context, u: User) -> Identity:
        """Known users and admins resolve; an email containing "ambiguous"
        is ambiguous; everything else has no account."""
        if self.fail is not None:
            raise errorf(self.fail, "fake failure")
        email = u.email.lower()
        if "ambiguous" in email:
            raise user_ambiguous(f"several accounts match {u.email}")
        if email not in self.users and email not in self.admins:
            raise user_not_found(f"no account for {u.email}")
        role = "admin" if email in self.admins else "user"
        return Identity(id=email, display=email, attrs={"role": role})

    def check(self, ctx: Context, r: CheckRequest) -> Decision:
        """The three actions on "thing:<id>". The id "hidden" is not
        visible; "broken" is unsupported."""
        if self.fail is not None:
            raise errorf(self.fail, "fake failure")
        if r.resource.type != "thing" or r.resource.id == "":
            raise errorf(Code.INVALID_REQUEST, "resource must be thing:<id>")
        if r.resource.id == "hidden":
            return unknown_decision(Code.RESOURCE_NOT_VISIBLE, f'thing "{r.resource.id}" is not visible to the fake credential')
        if r.resource.id == "broken":
            return unsupported(f'thing "{r.resource.id}" uses a policy the fake integration cannot evaluate')
        admin = r.identity.attr("role") == "admin"
        if r.action.name == "thing.read":
            return allowed(f"{r.identity.display} is a known user")
        if r.action.name in ("thing.write", "thing.admin"):
            if admin:
                return allowed(f"{r.identity.display} is an admin")
            return denied(f"{r.identity.display} is not an admin")
        raise RuntimeError("unreachable: unknown action")

    def probe(self, ctx: Context) -> ProbeResult:
        if self.fail is not None:
            raise errorf(self.fail, "fake failure")
        warnings: tuple[str, ...] = ()
        if not self.users and not self.admins:
            warnings = ("no users or admins configured; every check will answer user_not_found",)
        return ProbeResult(summary="fake integration ready", warnings=warnings)


INTEGRATION = Fake()

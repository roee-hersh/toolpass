"""groups_connection: where an argocd connection finds the user's groups when
the caller sends none (the Okta directory behind Argo CD's OIDC login)."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

import pytest

from tests import harness as itest
from tests.integrations.argocd.test_argocd import Cluster, check, default_cluster
from toolpass.core.config import parse as parse_config
from toolpass.core.context import Context, background
from toolpass.core.decision import Code, user_not_found
from toolpass.core.integration import Connection, KubernetesSubject, User
from toolpass.integrations import kubernetes, registry
from toolpass.integrations.argocd import Integration


@dataclass
class Source:
    """A KubernetesSubjectSource that records what it was asked."""

    subjects: dict[str, KubernetesSubject] = field(default_factory=dict)
    kubernetes_needs_cluster: bool = False
    calls: list[str] = field(default_factory=list)

    def kubernetes_subject(self, ctx: Context, email: str, cluster: str) -> KubernetesSubject:
        assert cluster == ""
        self.calls.append(email)
        s = self.subjects.get(email)
        if s is None:
            raise user_not_found(f"no account for {email}")
        return s


Make = Callable[..., Connection]


@pytest.fixture
def make() -> Iterator[Make]:
    servers: list[itest.Server] = []

    def build(src: Any, cl: Cluster | None = None, user_subject: str = "none") -> Connection:
        srv = itest.Server()
        servers.append(srv)
        (cl or default_cluster()).install(srv)
        deps, _ = itest.deps(srv)
        ks = itest.settings("k8s", "kubernetes", {"url": srv.url}, {"credential": itest.literal("k8s")})
        kc = kubernetes.Integration().new(background(), ks, deps)
        deps.connection = lambda id_: {"k8s": kc, "dir": src}[id_]
        s = itest.settings(
            "argo",
            "argocd",
            {
                "kubernetes_connection": "k8s",
                "namespace": "argocd",
                "rbac_configmap": "argocd-rbac-cm",
                "user_subject": user_subject,
                "groups_connection": "dir",
            },
        )
        return Integration().new(background(), s, deps)

    yield build
    for s in servers:
        s.close()


dana = User(email="dana-ops@example.com")


def test_source_groups_used_when_caller_sends_none(make: Make) -> None:
    src = Source({"dana-ops@example.com": KubernetesSubject(username="ignored", groups=("sre",))})
    c = make(src)
    d = check(c, dana, "app.delete", "applications:dev/web")
    itest.expect_code(d, Code.ALLOWED)
    assert "sre" in d.text, d
    itest.expect_code(check(c, dana, "app.delete", "applications:prod/web"), Code.DENIED)
    assert src.calls and set(src.calls) == {"dana-ops@example.com"}


def test_caller_groups_win(make: Make) -> None:
    src = Source({"dana-ops@example.com": KubernetesSubject(groups=("sre",))})
    c = make(src)
    u = User(email="dana-ops@example.com", groups=("developers",))
    itest.expect_code(check(c, u, "app.delete", "applications:dev/web"), Code.DENIED)
    itest.expect_code(check(c, u, "app.sync", "applications:dev/web"), Code.ALLOWED)
    assert src.calls == []


def test_project_role_groups_from_source(make: Make) -> None:
    c = make(Source({"a@example.com": KubernetesSubject(groups=("team-a-devs",))}))
    itest.expect_code(check(c, User(email="a@example.com"), "app.sync", "applications:team-a/web"), Code.ALLOWED)


def test_user_in_no_group_gets_the_default_role(make: Make) -> None:
    cl = default_cluster()
    assert cl.rbac_cm is not None
    cl.rbac_cm["policy.default"] = "role:readonly"
    c = make(Source({"new@example.com": KubernetesSubject()}), cl)
    u = User(email="new@example.com")
    itest.expect_code(check(c, u, "app.get", "applications:prod/web"), Code.ALLOWED)
    itest.expect_code(check(c, u, "app.sync", "applications:prod/web"), Code.DENIED)


def test_user_not_in_source(make: Make) -> None:
    c = make(Source())
    itest.expect_code(check(c, dana, "app.get", "applications:dev/web"), Code.USER_NOT_FOUND)


def test_email_scope_and_subject_still_apply(make: Make) -> None:
    # The policy's g line for dana@example.com reaches her through the email
    # scope, with or without groups from the directory.
    c = make(Source({"dana@example.com": KubernetesSubject(groups=("developers",))}))
    itest.expect_code(check(c, User(email="dana@example.com"), "app.sync", "applications:prod/web"), Code.ALLOWED)


def test_source_must_be_a_directory(make: Make) -> None:
    with pytest.raises(ValueError, match="cannot look up the groups of an OIDC login"):
        make(object())
    with pytest.raises(ValueError, match="cannot look up the groups of an OIDC login"):
        make(Source(kubernetes_needs_cluster=True))


def test_config_accepts_okta_and_fake_and_rejects_others() -> None:
    base = (
        "api_key: env:K\nconnections:\n"
        "  - id: k\n    integration: kubernetes\n    url: https://k8s.example.com\n    credential: env:T\n"
        "  - id: a\n    integration: argocd\n    kubernetes_connection: k\n"
    )
    ok = base + "    groups_connection: d\n  - id: d\n    integration: fake\n    groups: dana@example.com=sre\n"
    cfg = parse_config("t.yaml", ok, registry())
    ids = [s.id for s in cfg.connections]
    assert ids.index("d") < ids.index("a"), "the source must be built first"
    ok = base + "    groups_connection: o\n  - id: o\n    integration: okta\n    url: https://acme.okta.com\n    credential: env:O\n"
    parse_config("t.yaml", ok, registry())
    bad = base + "    groups_connection: j\n  - id: j\n    integration: jira\n    url: https://x.atlassian.net\n    credential: env:J\n    username: a@b.c\n"
    with pytest.raises(Exception, match=r"groups_connection must name a okta, fake connection, but \"j\" is jira"):
        parse_config("t.yaml", bad, registry())

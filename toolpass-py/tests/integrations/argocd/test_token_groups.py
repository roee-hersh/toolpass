"""argocd with oidc_issuer: the groups of the ID token the check carries
decide, for whatever provider signed it."""

from __future__ import annotations

import pytest

from tests import harness as itest
from tests.core.test_oidc import AUD, Provider
from tests.integrations.argocd.test_argocd import check, default_cluster
from toolpass.core.context import background
from toolpass.core.decision import Code
from toolpass.core.integration import User
from toolpass.integrations import kubernetes
from toolpass.integrations.argocd import Integration

pytest.importorskip("cryptography")


def test_groups_from_the_token() -> None:
    sso, srv = itest.Server(), itest.Server()
    try:
        p = Provider(sso)
        default_cluster().install(srv)
        deps, _ = itest.deps(srv)
        ks = itest.settings("k8s", "kubernetes", {"url": srv.url}, {"credential": itest.literal("k8s")})
        kc = kubernetes.Integration().new(background(), ks, deps)
        deps.connection = lambda _id: kc
        s = itest.settings(
            "argo",
            "argocd",
            {
                "kubernetes_connection": "k8s",
                "namespace": "argocd",
                "rbac_configmap": "argocd-rbac-cm",
                "user_subject": "none",
                "oidc_issuer": p.issuer,
                "oidc_audience": AUD,
            },
        )
        argo = Integration().new(background(), s, deps)
        # (dana@example.com has a g line of her own in the policy; use another.)
        email = "sam@example.com"
        sam = User(email=email, token=p.token(email=email, groups=["sre"]))
        d = check(argo, sam, "app.delete", "applications:dev/web")
        itest.expect_code(d, Code.ALLOWED)
        assert "sre" in d.text and "eyJ" not in d.text, d
        dev = User(email=email, token=p.token(email=email, groups=["developers"]))
        itest.expect_code(check(argo, dev, "app.delete", "applications:dev/web"), Code.DENIED)
        itest.expect_code(check(argo, dev, "app.sync", "applications:dev/web"), Code.ALLOWED)
        # No token: unknown, never a deny or an allow.
        itest.expect_code(check(argo, User(email=email), "app.sync", "applications:dev/web"), Code.INVALID_REQUEST)
        # A token for someone else's email.
        itest.expect_code(
            check(argo, User(email="bob@example.com", token=p.token(email=email, groups=["sre"])), "app.sync", "applications:dev/web"), Code.INVALID_REQUEST
        )
        # Caller-sent groups still win over the token.
        both = User(email=email, groups=("developers",), token=p.token(email=email, groups=["sre"]))
        itest.expect_code(check(argo, both, "app.delete", "applications:dev/web"), Code.DENIED)
        r = argo.probe(background())
        assert "signing key" in r.summary, r
        # Without oidc_issuer nothing changes: no groups means no groups.
        plain = Integration().new(
            background(),
            itest.settings(
                "argo", "argocd", {"kubernetes_connection": "k8s", "namespace": "argocd", "rbac_configmap": "argocd-rbac-cm", "user_subject": "none"}
            ),
            deps,
        )
        itest.expect_code(check(plain, sam, "app.delete", "applications:dev/web"), Code.DENIED)
    finally:
        sso.close()
        srv.close()

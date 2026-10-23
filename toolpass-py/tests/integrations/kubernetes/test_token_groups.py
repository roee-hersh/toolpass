"""kubernetes with oidc_issuer: the groups of the ID token the check carries
reach the SubjectAccessReview, with group_prefix in front."""

from __future__ import annotations

import pytest

from tests import harness as itest
from tests.core.test_oidc import AUD, Provider
from tests.integrations.kubernetes.test_kubernetes import RULES, SAR, FakeAPI, rules_handler
from toolpass.core.context import background
from toolpass.core.decision import Code
from toolpass.core.integration import User
from toolpass.integrations.kubernetes import Integration

pytest.importorskip("cryptography")


def test_groups_from_the_token() -> None:
    sso, srv = itest.Server(), itest.Server()
    try:
        p = Provider(sso)
        api = FakeAPI({"oidc:platform-team": ["create deployments.apps payments "]})
        srv.handle("POST", SAR, api.handler)
        srv.handle("POST", RULES, rules_handler(None))
        deps, _ = itest.deps(srv)
        values = {"url": srv.url, "group_prefix": "oidc:", "oidc_issuer": p.issuer, "oidc_audience": AUD}
        c = Integration().new(background(), itest.settings("k8s", "kubernetes", values, {"credential": itest.literal("k8s")}), deps)
        dana = User(email="dana@example.com", token=p.token(groups=["platform-team"]))
        itest.expect_code(itest.check(c, Integration(), dana, "deployment.create", "namespace:payments"), Code.ALLOWED)
        spec = [x for x in srv.calls() if x.path == SAR][-1].json()["spec"]
        assert spec["groups"] == ["oidc:platform-team", "system:authenticated"], spec
        # No token: unknown.
        itest.expect_code(itest.check(c, Integration(), User(email="dana@example.com"), "deployment.create", "namespace:payments"), Code.INVALID_REQUEST)
        # Without a prefix a token group may still not name a reserved group.
        values = {"url": srv.url, "oidc_issuer": p.issuer, "oidc_audience": AUD}
        c = Integration().new(background(), itest.settings("k8s", "kubernetes", values, {"credential": itest.literal("k8s")}), deps)
        eve = User(email="dana@example.com", token=p.token(groups=["system:masters"]))
        itest.expect_code(itest.check(c, Integration(), eve, "raw:delete:nodes", "cluster"), Code.INVALID_REQUEST)
        # Not together with groups_connection.
        from toolpass.integrations.fake import Fake

        deps.connection = lambda _id: Fake().new(background(), itest.settings("dir", "fake", {"groups": "dana@example.com=platform-team"}), deps)
        values["groups_connection"] = "dir"
        with pytest.raises(ValueError, match="set one"):
            Integration().new(background(), itest.settings("k8s", "kubernetes", values, {"credential": itest.literal("k8s")}), deps)
    finally:
        sso.close()
        srv.close()

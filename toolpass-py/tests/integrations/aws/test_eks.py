"""The aws connection as a kubernetes groups_connection: an email's IAM
principals looked up as EKS access entries (eks:DescribeAccessEntry)."""

from __future__ import annotations

import pytest

from tests import harness as itest
from tests.integrations.aws.test_aws import (  # noqa: F401 - the fixtures
    ACCT,
    ROLE_DEV,
    ROLE_RO,
    Env,
    Setup,
    servers,
    setup,
)
from toolpass.core.context import background
from toolpass.core.decision import Code, ToolpassError
from toolpass.core.integration import KubernetesSubject, User
from toolpass.integrations.aws.eks import EKSMapping, arn_candidates, match_aws_auth, merge_mappings, parse_aws_auth, render_username

RO_SHORT = "arn:aws:iam::123456789012:role/AWSReservedSSO_ReadOnly_0123456789abcdef"
DEV_SHORT = "arn:aws:iam::123456789012:role/AWSReservedSSO_Developer_fedcba9876543210"


def subject(e: Env, email: str, cluster: str = "prod") -> KubernetesSubject:
    return e.c.kubernetes_subject(background(), User(email=email), cluster)


def code_of(e: Env, email: str, cluster: str = "prod") -> tuple[Code, str]:
    with pytest.raises(ToolpassError) as ei:
        subject(e, email, cluster)
    return ei.value.code, ei.value.text


def test_identity_center_roles_matched_without_their_path(setup: Setup) -> None:  # noqa: F811
    e = setup()
    # dana has ReadOnly (direct) and Developer (through a group).
    e.f.eks_entries = {
        "prod": {
            RO_SHORT: {"kubernetesGroups": ["viewers"], "username": "dana-ro"},
            DEV_SHORT: {"kubernetesGroups": ["platform-team", "viewers"], "username": "dana-dev"},
        }
    }
    s = subject(e, "dana@example.com")
    assert s.mapped and s.found
    assert s.groups == ("viewers", "platform-team"), s
    assert s.username == "", "two entries matched, so no single username applies"
    assert s.tried == (), "every principal had an entry"
    # Each ARN tried as is first, then without the path.
    first = e.f.eks_calls[0]
    assert e.f.eks_calls[:2] == [first, arn_candidates(first)[1]], e.f.eks_calls


def test_full_arn_entry_and_single_username(setup: Setup) -> None:  # noqa: F811
    e = setup()
    e.f.eks_entries = {"prod": {ROLE_RO: {"kubernetesGroups": ["system:masters"], "username": "sso:{{SessionName}}"}}}
    s = subject(e, "dana@example.com")
    assert s.groups == ("system:masters",)
    assert s.username == "sso:dana-example.com", s
    # The full ARN matched, so the short form was not asked for it; the
    # Developer role has no entry and is left for aws-auth.
    assert RO_SHORT not in e.f.eks_calls
    assert s.tried == (ROLE_DEV,), s


def test_no_entry_is_not_found_with_the_principals(setup: Setup) -> None:  # noqa: F811
    e = setup()
    e.f.eks_entries = {"prod": {}}
    s = subject(e, "dana@example.com")
    assert not s.found and s.mapped and set(s.tried) == {ROLE_RO, ROLE_DEV}, s
    # An unknown cluster reads the same (ResourceNotFoundException).
    s = subject(e, "dana@example.com", "other")
    assert not s.found


def test_config_map_only_cluster(setup: Setup) -> None:  # noqa: F811
    e = setup()
    e.f.eks_err = "InvalidRequestException"
    s = subject(e, "dana@example.com")
    assert not s.found


def test_users_without_principals(setup: Setup) -> None:  # noqa: F811
    e = setup()
    code, text = code_of(e, "bob@example.com")
    assert code == Code.USER_NOT_FOUND and "no IAM principal" in text, text
    code, _ = code_of(e, "nobody@example.com")
    assert code == Code.USER_NOT_FOUND
    code, text = code_of(e, "off@example.com")
    assert code == Code.USER_NOT_FOUND and "disabled" in text, text
    assert e.f.eks_calls == []


def test_errors_are_classified(setup: Setup) -> None:  # noqa: F811
    e = setup()
    e.f.eks_entries = {"prod": {}}
    e.f.eks_err = "AccessDeniedException"
    code, text = code_of(e, "dana@example.com")
    assert code == Code.CREDENTIAL_REJECTED and "DescribeAccessEntry" in text, text
    e.f.eks_err = "ServerException"
    code, _ = code_of(e, "dana@example.com")
    assert code == Code.UPSTREAM_ERROR


def test_bad_cluster_name(setup: Setup) -> None:  # noqa: F811
    e = setup()
    with pytest.raises(ValueError):
        subject(e, "dana@example.com", "bad/../name")


def test_iam_user_mode(setup: Setup) -> None:  # noqa: F811
    e = setup({"identity_mode": "iam_user"})
    e.f.eks_entries = {"prod": {"arn:aws:iam::123456789012:user/dana": {"kubernetesGroups": ["platform-team"], "username": "dana"}}}
    s = subject(e, "dana@example.com")
    assert s.groups == ("platform-team",) and s.username == "dana", s


def test_static_map_mode(setup: Setup, tmp_path: object) -> None:  # noqa: F811
    import pathlib

    p = pathlib.Path(str(tmp_path)) / "map"
    p.write_text("dana@example.com arn:aws:iam::123456789012:role/Deployer\nplatform-team arn:aws:iam::123456789012:role/PlatformAdmin\n")
    e = setup({"identity_mode": "static_map", "role_map_file": str(p), "identity_center_role_arn": "", "identity_store_id": "", "sso_instance_arn": ""})
    e.f.eks_entries = {"prod": {"arn:aws:iam::123456789012:role/Deployer": {"kubernetesGroups": ["deployers"]}}}
    s = subject(e, "dana@example.com")
    # Only the email's own rows: group rows need groups the caller did not send.
    assert s.groups == ("deployers",) and s.found and s.tried == (), s
    code, _ = code_of(e, "bob@example.com")
    assert code == Code.USER_NOT_FOUND


def test_eks_region_and_signing(setup: Setup) -> None:  # noqa: F811
    e = setup()
    e.f.eks_entries = {"prod": {RO_SHORT: {"kubernetesGroups": ["viewers"]}}}
    subject(e, "dana@example.com")
    calls = [c for c in e.srv.calls() if c.path.startswith("/clusters/")]
    assert calls, "no EKS call"
    # A different eks_region is signed for that region (the fake refuses it).
    e = setup({"eks_region": "us-east-1"})
    e.f.eks_entries = {"prod": {RO_SHORT: {"kubernetesGroups": ["viewers"]}}}
    code, _ = code_of(e, "dana@example.com")
    assert code == Code.CREDENTIAL_REJECTED


def test_kubernetes_end_to_end_with_aws(setup: Setup) -> None:  # noqa: F811
    """kubernetes(groups_connection=aws) against the fake API server."""
    from tests.integrations.kubernetes.test_kubernetes import RULES, SAR, FakeAPI, rules_handler
    from toolpass.integrations.kubernetes import Integration as K8s

    e = setup()
    e.f.eks_entries = {"prod": {DEV_SHORT: {"kubernetesGroups": ["platform-team"], "username": "arn:aws:sts::{{AccountID}}:assumed-role/Dev/{{SessionName}}"}}}
    k = itest.Server()
    try:
        api = FakeAPI({"platform-team": ["create deployments.apps payments "]})
        k.handle("POST", SAR, api.handler)
        k.handle("POST", RULES, rules_handler(None))
        deps, _ = itest.deps(k, connection=lambda _id: e.conn)
        s = itest.settings("k8s", "kubernetes", {"url": k.url, "groups_connection": "aws-prod", "eks_cluster": "prod"}, {"credential": itest.literal("k8s")})
        c = K8s().new(background(), s, deps)
        d = itest.check(c, K8s(), User(email="dana@example.com"), "deployment.create", "namespace:payments")
        itest.expect_code(d, Code.ALLOWED)
        spec = k.last_call().json()["spec"]
        assert spec["user"] == f"arn:aws:sts::{ACCT}:assumed-role/Dev/dana-example.com", spec
        assert spec["groups"] == ["platform-team", "system:authenticated"], spec
        d = itest.check(c, K8s(), User(email="bob@example.com"), "deployment.create", "namespace:payments")
        itest.expect_code(d, Code.USER_NOT_FOUND)
        # Without eks_cluster the aws source is refused at build time.
        s = itest.settings("k8s", "kubernetes", {"url": k.url, "groups_connection": "aws-prod"}, {"credential": itest.literal("k8s")})
        with pytest.raises(ValueError, match="eks_cluster is required"):
            K8s().new(background(), s, deps)
    finally:
        k.close()


# -- the pure helpers ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("arn", "want"),
    [
        ("arn:aws:iam::1:role/x", ["arn:aws:iam::1:role/x"]),
        ("arn:aws:iam::123456789012:role/Dev", ["arn:aws:iam::123456789012:role/Dev"]),
        (ROLE_DEV, [ROLE_DEV, DEV_SHORT]),
        ("arn:aws-us-gov:iam::123456789012:user/team/ops", ["arn:aws-us-gov:iam::123456789012:user/team/ops", "arn:aws-us-gov:iam::123456789012:user/ops"]),
        ("not an arn", ["not an arn"]),
    ],
)
def test_arn_candidates(arn: str, want: list[str]) -> None:
    assert arn_candidates(arn) == want


@pytest.mark.parametrize(
    ("username", "want"),
    [
        ("", ""),
        ("dana", "dana"),
        ("sso:{{SessionName}}", "sso:dana-example.com"),
        ("{{SessionNameRaw}}", "dana@example.com"),
        ("arn:aws:sts::{{AccountID}}:assumed-role/X/{{SessionName}}", "arn:aws:sts::123456789012:assumed-role/X/dana-example.com"),
        ("system:node:{{EC2PrivateDNSName}}", ""),
        ("{{AccessKeyID}}", ""),
        ("x{{", ""),
    ],
)
def test_render_username(username: str, want: str) -> None:
    assert render_username(username, "arn:aws:iam::123456789012:role/X", "dana@example.com") == want


def test_merge_mappings() -> None:
    a = EKSMapping("p1", "u1", ("a", "b"))
    b = EKSMapping("p2", "u2", ("b", "c", ""))
    assert merge_mappings([a], "e@x") == ("u1", ("a", "b"))
    assert merge_mappings([a, b], "e@x") == ("", ("a", "b", "c"))


def test_aws_auth_parsing() -> None:
    data = {
        "mapRoles": "- rolearn: arn:aws:iam::1:role/A\n  username: a\n  groups: [g1, 7]\n- 'junk'\n- rolearn: ''\n- {username: nobody}\n",
        "mapUsers": "- userarn: arn:aws:iam::1:user/u\n  groups: [g2]\n",
        "mapAccounts": "- '1'\n",
    }
    assert parse_aws_auth(data) == [("arn:aws:iam::1:role/A", "a", ("g1",)), ("arn:aws:iam::1:user/u", "", ("g2",))]
    assert parse_aws_auth({}) == []
    assert parse_aws_auth({"mapRoles": "{}"}) == []
    with pytest.raises(ValueError, match="mapRoles is not YAML"):
        parse_aws_auth({"mapRoles": "- [unclosed"})
    # The SSO role is found without its path; the first row for an ARN wins.
    data = {"mapRoles": f"- rolearn: {DEV_SHORT}\n  groups: [dev]\n- rolearn: {DEV_SHORT}\n  groups: [second]\n"}
    assert match_aws_auth(data, [ROLE_DEV, RO_SHORT]) == [EKSMapping(ROLE_DEV, "", ("dev",))]

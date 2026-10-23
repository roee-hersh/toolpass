"""groups_connection: where a kubernetes connection finds the user's groups
when the caller sends none (an EKS access entry, a directory)."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

import pytest

from tests import harness as itest
from tests.integrations.kubernetes.test_kubernetes import RULES, SAR, FakeAPI, rules_handler
from toolpass.core.config import parse as parse_config
from toolpass.core.context import Context, background
from toolpass.core.decision import Code, Decision, user_not_found
from toolpass.core.integration import Connection, KubernetesSubject, KubernetesSubjectSource, User
from toolpass.integrations import fake, registry
from toolpass.integrations.kubernetes import AWS_AUTH_PATH, Integration

AWS_AUTH = {
    "apiVersion": "v1",
    "kind": "ConfigMap",
    "metadata": {"name": "aws-auth", "namespace": "kube-system"},
    "data": {
        "mapRoles": (
            "- rolearn: arn:aws:iam::123456789012:role/AWSReservedSSO_Dev_0123456789abcdef\n"
            "  username: sso:{{SessionName}}\n"
            "  groups: [platform-team]\n"
            "- rolearn: arn:aws:iam::123456789012:role/Nodes\n"
            "  username: system:node:{{EC2PrivateDNSName}}\n"
            "  groups: [system:bootstrappers, system:nodes]\n"
        ),
        "mapUsers": "- userarn: arn:aws:iam::123456789012:user/ops\n  username: ops\n  groups:\n    - system:masters\n",
    },
}


@dataclass
class Source:
    """A KubernetesSubjectSource that records what it was asked."""

    subjects: dict[str, KubernetesSubject] = field(default_factory=dict)
    kubernetes_needs_cluster: bool = False
    calls: list[tuple[str, str]] = field(default_factory=list)

    def kubernetes_subject(self, ctx: Context, email: str, cluster: str) -> KubernetesSubject:
        self.calls.append((email, cluster))
        s = self.subjects.get(email)
        if s is None:
            raise user_not_found(f"no account for {email}")
        return s


Make = Callable[..., "tuple[itest.Server, FakeAPI, Connection]"]


@pytest.fixture
def make() -> Iterator[Make]:
    servers: list[itest.Server] = []

    def build(
        src: Any, values: dict[str, str] | None = None, aws_auth: Any = None, rules: list[dict[str, Any]] | None = None
    ) -> tuple[itest.Server, FakeAPI, Connection]:
        srv = itest.Server()
        servers.append(srv)
        api = FakeAPI(
            {
                "dana@example.com": ["get pods payments "],
                "platform-team": ["create deployments.apps payments "],
                "oidc:platform-team": ["create pods/exec payments "],
                "system:masters": [""],
                "sso:dana-example.com": ["get secrets payments "],
            }
        )
        srv.handle("POST", SAR, api.handler)
        srv.handle("POST", RULES, rules_handler(rules))
        if aws_auth is not None:
            srv.json("GET", AWS_AUTH_PATH, 200, aws_auth)
        deps, _ = itest.deps(srv, connection=lambda _id: src)
        v = {"url": srv.url, "groups_connection": "dir"}
        v.update(values or {})
        s = itest.settings("k8s", "kubernetes", v, {"credential": itest.literal("k8s")})
        return srv, api, Integration().new(background(), s, deps)

    yield build
    for s in servers:
        s.close()


def check(c: Connection, u: User, action: str, resource: str) -> Decision:
    return itest.check(c, Integration(), u, action, resource)


def last_spec(srv: itest.Server) -> dict[str, Any]:
    calls = [c for c in srv.calls() if c.path == SAR]
    assert calls, "no SubjectAccessReview was posted"
    spec: dict[str, Any] = calls[-1].json()["spec"]
    return spec


dana = User(email="dana@example.com")


def test_source_groups_used_when_caller_sends_none(make: Make) -> None:
    src = Source({"dana@example.com": KubernetesSubject(groups=("platform-team",))})
    srv, _, c = make(src)
    itest.expect_code(check(c, dana, "deployment.create", "namespace:payments"), Code.ALLOWED)
    spec = last_spec(srv)
    assert spec["user"] == "dana@example.com" and spec["groups"] == ["platform-team", "system:authenticated"], spec
    assert src.calls == [("dana@example.com", "")]


def test_caller_groups_win(make: Make) -> None:
    src = Source({"dana@example.com": KubernetesSubject(groups=("platform-team",))})
    srv, _, c = make(src)
    itest.expect_code(check(c, User(email="dana@example.com", groups=("viewers",)), "deployment.create", "namespace:payments"), Code.DENIED)
    assert last_spec(srv)["groups"] == ["viewers", "system:authenticated"]
    assert src.calls == [], "the source must not be asked when the caller sends groups"


def test_directory_groups_get_the_prefix_and_reserved_check(make: Make) -> None:
    src = Source({"dana@example.com": KubernetesSubject(groups=("platform-team",)), "eve@example.com": KubernetesSubject(groups=("system:masters",))})
    srv, _, c = make(src, {"group_prefix": "oidc:"})
    itest.expect_code(check(c, dana, "pods.exec", "namespace:payments?name=api-0"), Code.ALLOWED)
    assert last_spec(srv)["groups"] == ["oidc:platform-team", "system:authenticated"]
    # Without a prefix a directory group named system:... is refused, as a
    # caller's would be: a directory admin must not mint cluster-admin.
    srv, _, c = make(src)
    d = check(c, User(email="eve@example.com"), "raw:delete:nodes", "cluster")
    itest.expect_code(d, Code.INVALID_REQUEST)
    assert "system:masters" in d.text and "groups_connection" in d.text, d
    assert not [x for x in srv.calls() if x.path == SAR], "review sent with a reserved group"


def test_mapped_groups_are_sent_as_they_are(make: Make) -> None:
    src = Source({"dana@example.com": KubernetesSubject(username="dana", groups=("system:masters", "platform-team"), mapped=True)})
    srv, _, c = make(src, {"group_prefix": "oidc:"})
    itest.expect_code(check(c, dana, "raw:delete:nodes", "cluster"), Code.ALLOWED)
    spec = last_spec(srv)
    assert spec["user"] == "dana" and spec["groups"] == ["system:masters", "platform-team", "system:authenticated"], spec


def test_source_username_must_not_be_reserved(make: Make) -> None:
    src = Source({"dana@example.com": KubernetesSubject(username="system:node:ip-10-0-0-1", groups=("system:nodes",), mapped=True)})
    _, _, c = make(src)
    itest.expect_code(check(c, dana, "raw:get:pods", "namespace:payments"), Code.INVALID_REQUEST)


def test_no_duplicate_authenticated_group(make: Make) -> None:
    src = Source({"dana@example.com": KubernetesSubject(groups=("system:authenticated", "platform-team"), mapped=True)})
    srv, _, c = make(src)
    itest.expect_code(check(c, dana, "deployment.create", "namespace:payments"), Code.ALLOWED)
    assert last_spec(srv)["groups"] == ["system:authenticated", "platform-team"]


def test_user_not_in_source(make: Make) -> None:
    srv, _, c = make(Source())
    d = check(c, dana, "raw:get:pods", "namespace:payments")
    itest.expect_code(d, Code.USER_NOT_FOUND)
    assert not [x for x in srv.calls() if x.path == SAR]


def test_user_in_no_group_is_checked_by_name(make: Make) -> None:
    srv, _, c = make(Source({"dana@example.com": KubernetesSubject()}))
    itest.expect_code(check(c, dana, "raw:get:pods", "namespace:payments"), Code.ALLOWED)
    assert last_spec(srv)["groups"] == ["system:authenticated"]


def test_no_access_entry_names_the_principals_tried(make: Make) -> None:
    tried = ("arn:aws:iam::123456789012:role/aws-reserved/sso.amazonaws.com/AWSReservedSSO_Dev_0123456789abcdef",)
    src = Source({"dana@example.com": KubernetesSubject(found=False, mapped=True, tried=tried)}, kubernetes_needs_cluster=True)
    _, _, c = make(src, {"eks_cluster": "prod"})
    d = check(c, dana, "raw:get:pods", "namespace:payments")
    itest.expect_code(d, Code.USER_NOT_FOUND)
    assert "EKS cluster prod" in d.text and tried[0] in d.text, d
    assert src.calls == [("dana@example.com", "prod")]


def test_aws_auth_fallback(make: Make) -> None:
    tried = (
        "arn:aws:iam::123456789012:role/aws-reserved/sso.amazonaws.com/eu-west-1/AWSReservedSSO_Dev_0123456789abcdef",
        "arn:aws:iam::123456789012:role/Unmapped",
    )
    src = Source({"dana@example.com": KubernetesSubject(found=False, mapped=True, tried=tried)}, kubernetes_needs_cluster=True)
    srv, _, c = make(src, {"eks_cluster": "prod", "eks_aws_auth": "true"}, aws_auth=AWS_AUTH)
    itest.expect_code(check(c, dana, "deployment.create", "namespace:payments"), Code.ALLOWED)
    spec = last_spec(srv)
    # The SSO role matched without its path; exactly one row matched, so
    # its username (with {{SessionName}} filled) applies.
    assert spec["user"] == "sso:dana-example.com" and spec["groups"] == ["platform-team", "system:authenticated"], spec
    itest.expect_code(check(c, dana, "secrets.read", "namespace:payments?name=x"), Code.ALLOWED)


def test_aws_auth_for_the_principals_without_an_entry(make: Make) -> None:
    """EKS falls back to aws-auth per principal: one role has an access
    entry, the other only an aws-auth row; both count."""
    sso = "arn:aws:iam::123456789012:role/aws-reserved/sso.amazonaws.com/AWSReservedSSO_Dev_0123456789abcdef"
    src = Source(
        {"dana@example.com": KubernetesSubject(username="dana-entry", groups=("viewers",), mapped=True, tried=(sso,))},
        kubernetes_needs_cluster=True,
    )
    srv, _, c = make(src, {"eks_cluster": "prod", "eks_aws_auth": "true"}, aws_auth=AWS_AUTH)
    itest.expect_code(check(c, dana, "deployment.create", "namespace:payments"), Code.ALLOWED)
    spec = last_spec(srv)
    # Two mappings, so no single username: the template's applies.
    assert spec["user"] == "dana@example.com" and spec["groups"] == ["viewers", "platform-team", "system:authenticated"], spec
    # Without eks_aws_auth the entry alone answers.
    srv, _, c = make(src, {"eks_cluster": "prod"}, aws_auth=AWS_AUTH)
    itest.expect_code(check(c, dana, "deployment.create", "namespace:payments"), Code.DENIED)
    assert last_spec(srv)["user"] == "dana-entry"
    assert not [x for x in srv.calls() if x.path == AWS_AUTH_PATH]


def test_aws_auth_is_reused_across_users(make: Make) -> None:
    role = ("arn:aws:iam::123456789012:role/AWSReservedSSO_Dev_0123456789abcdef",)
    src = Source(
        {
            "dana@example.com": KubernetesSubject(found=False, mapped=True, tried=role),
            "lee@example.com": KubernetesSubject(found=False, mapped=True, tried=role),
        },
        kubernetes_needs_cluster=True,
    )
    srv, _, c = make(src, {"eks_cluster": "prod", "eks_aws_auth": "true"}, aws_auth=AWS_AUTH)
    itest.expect_code(check(c, dana, "deployment.create", "namespace:payments"), Code.ALLOWED)
    itest.expect_code(check(c, User(email="lee@example.com"), "deployment.create", "namespace:payments"), Code.ALLOWED)
    assert len([x for x in srv.calls() if x.path == AWS_AUTH_PATH]) == 1


def test_aws_auth_user_row_and_no_match(make: Make) -> None:
    ops = User(email="ops@example.com")
    src = Source(
        {
            "ops@example.com": KubernetesSubject(found=False, mapped=True, tried=("arn:aws:iam::123456789012:user/ops",)),
            "dana@example.com": KubernetesSubject(found=False, mapped=True, tried=("arn:aws:iam::123456789012:role/Other",)),
        },
        kubernetes_needs_cluster=True,
    )
    srv, _, c = make(src, {"eks_cluster": "prod", "eks_aws_auth": "true"}, aws_auth=AWS_AUTH)
    itest.expect_code(check(c, ops, "raw:delete:nodes", "cluster"), Code.ALLOWED)
    assert last_spec(srv)["user"] == "ops" and last_spec(srv)["groups"][0] == "system:masters"
    d = check(c, dana, "raw:get:pods", "namespace:payments")
    itest.expect_code(d, Code.USER_NOT_FOUND)
    assert "aws-auth" in d.text and "role/Other" in d.text, d


def test_aws_auth_missing_or_unreadable(make: Make) -> None:
    src = Source({"dana@example.com": KubernetesSubject(found=False, mapped=True, tried=("arn:aws:iam::123456789012:role/X",))}, kubernetes_needs_cluster=True)
    _, _, c = make(src, {"eks_cluster": "prod", "eks_aws_auth": "true"})
    itest.expect_code(check(c, dana, "raw:get:pods", "namespace:payments"), Code.USER_NOT_FOUND)
    srv, _, c = make(src, {"eks_cluster": "prod", "eks_aws_auth": "true"})
    srv.json("GET", AWS_AUTH_PATH, 403, {"kind": "Status", "code": 403})
    itest.expect_code(check(c, dana, "raw:get:pods", "namespace:payments"), Code.CREDENTIAL_REJECTED)
    bad = {"data": {"mapRoles": "- rolearn: [unclosed"}}
    _, _, c = make(src, {"eks_cluster": "prod", "eks_aws_auth": "true"}, aws_auth=bad)
    itest.expect_code(check(c, dana, "raw:get:pods", "namespace:payments"), Code.UPSTREAM_ERROR)


def test_probe_accepts_the_aws_auth_rule(make: Make) -> None:
    rule = {"verbs": ["get"], "apiGroups": [""], "resources": ["configmaps"], "resourceNames": ["aws-auth"]}
    src = Source(kubernetes_needs_cluster=True)
    _, _, c = make(src, {"eks_cluster": "prod", "eks_aws_auth": "true"}, aws_auth=AWS_AUTH, rules=[rule])
    r = c.probe(background())
    assert r.warnings == (), r
    # Without eks_aws_auth the same rule is over-privilege.
    _, _, c = make(Source(), {}, rules=[rule])
    r = c.probe(background())
    assert any("configmaps" in w for w in r.warnings), r
    # And with it, a cluster with no aws-auth ConfigMap is worth a warning.
    _, _, c = make(src, {"eks_cluster": "prod", "eks_aws_auth": "true"}, rules=[rule])
    r = c.probe(background())
    assert any("no kube-system/aws-auth" in w for w in r.warnings), r


@pytest.mark.parametrize(
    ("needs", "values", "want"),
    [
        (True, {}, "eks_cluster is required"),
        (False, {"eks_cluster": "prod"}, "eks_cluster is used only"),
        (False, {"eks_aws_auth": "true"}, "eks_aws_auth is used only"),
    ],
)
def test_field_combinations(make: Make, needs: bool, values: dict[str, str], want: str) -> None:
    with pytest.raises(ValueError, match=want):
        make(Source(kubernetes_needs_cluster=needs), values)


def test_eks_fields_need_groups_connection() -> None:
    srv = itest.Server()
    try:
        deps, _ = itest.deps(srv)
        s = itest.settings("k8s", "kubernetes", {"url": srv.url, "eks_cluster": "prod"}, {"credential": itest.literal("k8s")})
        with pytest.raises(ValueError, match="need groups_connection"):
            Integration().new(background(), s, deps)
    finally:
        srv.close()


def test_source_must_implement_the_protocol(make: Make) -> None:
    with pytest.raises(ValueError, match="cannot look up Kubernetes groups"):
        make(object())


@dataclass
class Directory:
    """A plain GroupsSource: group names, no username, no mapping."""

    groups: dict[str, tuple[str, ...]] = field(default_factory=dict)
    groups_claim_limit: int | None = None

    def user_groups(self, ctx: Context, user: User) -> tuple[str, ...]:
        g = self.groups.get(user.email)
        if g is None:
            raise user_not_found(f"no account for {user.email}")
        return g


def test_plain_groups_source(make: Make) -> None:
    srv, _, c = make(Directory({"dana@example.com": ("platform-team",), "eve@example.com": ("system:masters",)}))
    itest.expect_code(check(c, dana, "deployment.create", "namespace:payments"), Code.ALLOWED)
    assert last_spec(srv)["groups"] == ["platform-team", "system:authenticated"]
    # Directory groups are treated like groups a caller sent.
    itest.expect_code(check(c, User(email="eve@example.com"), "raw:delete:nodes", "cluster"), Code.INVALID_REQUEST)
    itest.expect_code(check(c, User(email="nobody@example.com"), "raw:get:pods", "namespace:payments"), Code.USER_NOT_FOUND)
    with pytest.raises(ValueError, match="eks_cluster is used only"):
        make(Directory(), {"eks_cluster": "prod"})
    capped = Directory({"dana@example.com": ("a", "b", "c")}, groups_claim_limit=2)
    _, _, c = make(capped)
    itest.expect_code(check(c, dana, "raw:get:pods", "namespace:payments"), Code.UNSUPPORTED)


def test_protocol_implementations() -> None:
    from toolpass.integrations.aws import AWSConnection

    assert isinstance(Source(), KubernetesSubjectSource)
    # issubclass does not work for protocols with data members.
    for cls, needs in ((fake.FakeConnection, False), (AWSConnection, True)):
        assert callable(getattr(cls, "kubernetes_subject", None)), cls
        assert cls.kubernetes_needs_cluster is needs, cls


def test_config_accepts_every_source_and_rejects_others() -> None:
    base = "api_key: env:K\nconnections:\n  - id: k\n    integration: kubernetes\n    url: https://k8s.example.com\n    credential: env:T\n"
    ok = base + "    groups_connection: d\n  - id: d\n    integration: fake\n    groups: dana@example.com=a,b;bob@example.com=c\n"
    cfg = parse_config("t.yaml", ok, registry())
    assert [s.id for s in cfg.connections] == ["d", "k"], "the source must be built first"
    bad = base + "    groups_connection: j\n  - id: j\n    integration: jira\n    url: https://x.atlassian.net\n    credential: env:J\n    username: a@b.c\n"
    with pytest.raises(Exception, match=r"groups_connection must name a aws, fake, googleworkspace, microsoft365, okta connection, but \"j\" is jira"):
        parse_config("t.yaml", bad, registry())


def test_end_to_end_through_the_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    """The kind e2e wiring (fake directory -> kubernetes) through config,
    engine and HTTP server, against the fake API server."""
    from toolpass.core.engine import Options, build
    from toolpass.server import Server

    srv = itest.Server()
    api = FakeAPI({"platform-team": ["create deployments.apps payments "], "system:masters": [""]})
    srv.handle("POST", SAR, api.handler)
    srv.handle("POST", RULES, rules_handler(None))
    ca = itest.test_ca().ca_file
    monkeypatch.setenv("TOOLPASS_API_KEY", "e2e")
    monkeypatch.setenv("K8S_TOKEN", itest.CANARY + "k8s")
    conn = f"    integration: kubernetes\n    url: {srv.url}\n    ca_file: {ca}\n    credential: env:K8S_TOKEN\n"
    yml = (
        "api_key: env:TOOLPASS_API_KEY\ndecision_log: none\nconnections:\n"
        "  - id: kind\n"
        + conn
        + "  - id: dir\n    integration: fake\n    users: dana@example.com,bob@example.com\n    groups: dana@example.com=platform-team\n"
        "  - id: kind-groups\n" + conn + "    groups_connection: dir\n"
        '  - id: dir-mapped\n    integration: fake\n    groups: ops@example.com=system:masters\n    groups_mapped: "true"\n'
        "  - id: kind-mapped\n" + conn + "    groups_connection: dir-mapped\n"
    )
    cfg = parse_config("e2e.yaml", yml, registry())
    eng = build(background(), cfg, Options())
    server = Server(eng, cfg.api_key)
    host, port = server.listen("127.0.0.1:0")
    server.serve_in_thread()
    import urllib.request

    def ask(conn_id: str, user: str, groups: list[str] | None, action: str, resource: str) -> str:
        body = json.dumps({"user": user, "groups": groups, "connection": conn_id, "action": action, "resource": resource}).encode()
        req = urllib.request.Request(f"http://{host}:{port}/check", data=body, method="POST", headers={"Authorization": "Bearer e2e"})
        with urllib.request.urlopen(req, timeout=30) as res:
            out: dict[str, Any] = json.loads(res.read())
        return str(out.get("decision", ""))

    try:
        assert ask("kind", "dana@example.com", None, "deployment.create", "namespace:payments") == "deny"
        assert ask("kind-groups", "dana@example.com", None, "deployment.create", "namespace:payments") == "allow"
        assert ask("kind-groups", "dana@example.com", ["viewers"], "deployment.create", "namespace:payments") == "deny"
        assert ask("kind-groups", "nobody@example.com", None, "raw:get:pods", "namespace:payments") == "deny"
        assert ask("kind-mapped", "ops@example.com", None, "raw:delete:nodes", "cluster") == "allow"
    finally:
        server.shutdown()
        srv.close()

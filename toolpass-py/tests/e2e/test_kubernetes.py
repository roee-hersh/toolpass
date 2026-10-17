"""Port of v0.5.0:test/e2e/kubernetes_test.go: tests that run against a real
Kubernetes API server. Skipped unless the environment names one
(TOOLPASS_E2E_KUBERNETES_URL, _TOKEN_FILE and _CA_FILE); test/kind/run.sh
sets up a kind cluster and exports them."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

import pytest

from toolpass.core.config import parse as parse_config
from toolpass.core.context import background
from toolpass.core.engine import Options, build
from toolpass.integrations import registry
from toolpass.server import Server

CASES = [
    ("dana@example.com", None, "raw:get:pods", "namespace:payments", "allow"),
    ("dana@example.com", None, "raw:list:pods", "namespace:payments", "allow"),
    ("dana@example.com", None, "pods.logs", "namespace:payments?name=api-0", "allow"),
    ("dana@example.com", None, "raw:delete:pods", "namespace:payments", "deny"),
    ("dana@example.com", None, "raw:get:pods", "namespace:billing", "deny"),
    ("dana@example.com", None, "deployment.create", "namespace:payments", "deny"),
    ("dana@example.com", ["platform-team"], "deployment.create", "namespace:payments", "allow"),
    ("dana@example.com", ["platform-team"], "scale", "namespace:payments?resource=deployments.apps&name=api", "allow"),
    ("dana@example.com", ["platform-team"], "pods.exec", "namespace:payments?name=api-0", "allow"),
    ("dana@example.com", ["platform-team"], "deployment.delete", "namespace:payments?name=api", "deny"),
    ("bob@example.com", None, "raw:get:pods", "namespace:payments", "allow"),
    ("bob@example.com", None, "raw:list:pods", "namespace:payments", "deny"),
    ("bob@example.com", None, "pods.logs", "namespace:payments", "deny"),
    ("nobody@example.com", None, "raw:get:pods", "namespace:payments", "deny"),
    ("nobody@example.com", None, "raw:get", "nonresource:/version", "allow"),
    ("nobody@example.com", None, "raw:get", "nonresource:/api", "allow"),
    ("nobody@example.com", None, "raw:post", "nonresource:/api", "deny"),
    ("nobody@example.com", None, "raw:get:nodes", "cluster", "deny"),
    ("nobody@example.com", None, "secrets.read", "namespace:kube-system?name=x", "deny"),
]

# The same cluster through connections whose groups come from a directory
# (the fake integration) when the caller sends none. dana is in
# platform-team only there; the fixtures grant deployment.create in
# payments only through that group, so it is allowed with no groups sent.
GROUP_CASES = [
    ("kind-groups", "dana@example.com", None, "deployment.create", "namespace:payments", "allow"),
    ("kind-groups", "dana@example.com", None, "pods.exec", "namespace:payments?name=api-0", "allow"),
    ("kind-groups", "dana@example.com", None, "deployment.create", "namespace:billing", "deny"),
    ("kind-groups", "dana@example.com", None, "deployment.delete", "namespace:payments?name=api", "deny"),
    # Groups the caller sends win over the directory's.
    ("kind-groups", "dana@example.com", ["viewers"], "deployment.create", "namespace:payments", "deny"),
    # bob is a known user in no directory group.
    ("kind-groups", "bob@example.com", None, "raw:get:pods", "namespace:payments", "allow"),
    ("kind-groups", "bob@example.com", None, "deployment.create", "namespace:payments", "deny"),
    # Not in the directory at all: user_not_found, a deny.
    ("kind-groups", "nobody@example.com", None, "raw:get:pods", "namespace:payments", "deny"),
    # Mapped groups (as an EKS access entry's) are Kubernetes group names
    # sent as they are, system:masters included.
    ("kind-mapped", "ops@example.com", None, "raw:delete:nodes", "cluster", "allow"),
    ("kind-mapped", "dana@example.com", None, "raw:delete:nodes", "cluster", "deny"),
]


def test_kubernetes(monkeypatch: pytest.MonkeyPatch) -> None:
    url = os.environ.get("TOOLPASS_E2E_KUBERNETES_URL", "")
    token_file = os.environ.get("TOOLPASS_E2E_KUBERNETES_TOKEN_FILE", "")
    ca_file = os.environ.get("TOOLPASS_E2E_KUBERNETES_CA_FILE", "")
    if url == "" or token_file == "" or ca_file == "":
        pytest.skip("TOOLPASS_E2E_KUBERNETES_* not set")
    monkeypatch.setenv("TOOLPASS_API_KEY", "e2e")
    yml = (
        "api_key: env:TOOLPASS_API_KEY\ndecision_log: none\nconnections:\n"
        f"  - id: kind\n    integration: kubernetes\n    url: {url}\n    ca_file: {ca_file}\n    credential: file:{token_file}\n"
        "  - id: dir\n    integration: fake\n    users: dana@example.com,bob@example.com\n    groups: dana@example.com=platform-team\n"
        f"  - id: kind-groups\n    integration: kubernetes\n    url: {url}\n    ca_file: {ca_file}\n    credential: file:{token_file}\n"
        "    groups_connection: dir\n"
        '  - id: dir-mapped\n    integration: fake\n    users: dana@example.com\n    groups: ops@example.com=system:masters\n    groups_mapped: "true"\n'
        f"  - id: kind-mapped\n    integration: kubernetes\n    url: {url}\n    ca_file: {ca_file}\n    credential: file:{token_file}\n"
        "    groups_connection: dir-mapped\n"
    )
    cfg = parse_config("e2e.yaml", yml, registry())
    eng = build(background(), cfg, Options())
    for p in eng.probe(background()):
        assert p.err is None, f"probe {p.id}: {p.err}"
        print(f"probe {p.id}: {p.result.summary} {list(p.result.warnings)}")
    srv = Server(eng, cfg.api_key)
    host, port = srv.listen("127.0.0.1:0")
    srv.serve_in_thread()
    base = f"http://{host}:{port}"

    def check(user: str, groups: list[str] | None, action: str, resource: str, connection: str = "kind") -> tuple[str, str]:
        body = json.dumps({"user": user, "groups": groups, "connection": connection, "action": action, "resource": resource}).encode()
        req = urllib.request.Request(base + "/check", data=body, method="POST", headers={"Authorization": "Bearer e2e"})
        try:
            with urllib.request.urlopen(req, timeout=30) as res:
                status, data = res.status, res.read()
        except urllib.error.HTTPError as e:
            status, data = e.code, e.read()
        out = json.loads(data)
        assert status == 200, f"{user} {action} {resource}: HTTP {status} {out.get('reason')}"
        return out.get("decision", ""), out.get("reason", "")

    errors = []
    try:
        for user, groups, action, resource, want in CASES:
            got, reason = check(user, groups, action, resource)
            if got != want:
                errors.append(f"{user} {groups} {action} {resource}: got {got} ({reason}), want {want}")
            else:
                print(f"{user} {groups} {action} {resource}: {got} ({reason})")
        for conn, user, groups, action, resource, want in GROUP_CASES:
            got, reason = check(user, groups, action, resource, conn)
            if got != want:
                errors.append(f"{conn}: {user} {groups} {action} {resource}: got {got} ({reason}), want {want}")
            else:
                print(f"{conn}: {user} {groups} {action} {resource}: {got} ({reason})")
    finally:
        srv.shutdown()
    assert not errors, "\n".join(errors)

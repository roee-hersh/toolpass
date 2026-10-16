"""The vault route table against Vault's API description, its refusals, and
its resources answered by the vault check itself."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from tests.harness.routes import check_routes_against_spec
from tests.integrations.vault.test_vault import Env, check, dana, ops
from toolpass.core.catalog import parse_resource
from toolpass.core.decision import Code, ToolpassError
from toolpass.core.integration import Settings, find_action
from toolpass.core.routes import Request
from toolpass.integrations.vault import Vault
from toolpass.integrations.vault.actions import parse_target
from toolpass.integrations.vault.routes import ROUTES


def req(method: str, path: str, query: str = "", ns: str | None = None) -> Request:
    headers = {"content-type": "application/json"} if ns is None else {"content-type": "application/json", "x-vault-namespace": ns}
    return Request("https", "vault.example", 8200, method, "/v1" + path, query, headers, b"{}" if method in ("POST", "PUT", "PATCH") else None)


def match(method: str, path: str, query: str = "", ns: str | None = None):
    return ROUTES.match(req(method, path, query, ns), "/v1")


@pytest.fixture
def env() -> Iterator[Env]:
    e = Env()
    yield e
    e.close()


def test_routes_match_the_api_description():
    assert check_routes_against_spec(ROUTES, "vault", host="vault.example") == []


def test_every_route_names_a_known_action():
    for r in ROUTES.routes:
        assert r.action is None or find_action(Vault(), r.action) is not None, r


def test_endpoint_is_the_url_and_v1():
    s = Settings("v", "vault", {"url": "https://vault.example:8200"})
    assert ROUTES.endpoints(s) == [("vault.example:8200", "/v1")]
    s = Settings("v", "vault", {"url": "https://proxy.example/vault/"})
    assert ROUTES.endpoints(s) == [("proxy.example", "/vault/v1")]


@pytest.mark.parametrize(
    ("method", "path", "query", "ns", "action", "resource", "effect"),
    [
        ("GET", "/secret/data/app", "", None, "raw:read", "path:secret/data/app?namespace=", "read"),
        ("HEAD", "/secret/data/app", "", None, "raw:read", "path:secret/data/app?namespace=", "read"),
        ("GET", "/secret/metadata/app", "list=true", None, "raw:list", "path:secret/metadata/app?namespace=", "read"),
        ("GET", "/secret/metadata/app/", "list=1", None, "raw:list", "path:secret/metadata/app?namespace=", "read"),
        ("GET", "/secret/metadata/app", "list=false", None, "raw:read", "path:secret/metadata/app?namespace=", "read"),
        ("LIST", "/secret/metadata/app/", "", None, "raw:list", "path:secret/metadata/app?namespace=", "read"),
        ("POST", "/secret/data/app", "", None, "secret.write", "path:secret/data/app?namespace=", "write"),
        ("PUT", "/kv1/app", "", None, "secret.write", "path:kv1/app?namespace=", "write"),
        ("PATCH", "/secret/data/app", "", None, "raw:patch", "path:secret/data/app?namespace=", "write"),
        ("DELETE", "/secret/data/app", "", None, "raw:delete", "path:secret/data/app?namespace=", "destructive"),
        ("PUT", "/secret/destroy/app", "", None, "secret.write", "path:secret/destroy/app?namespace=", "destructive"),
        ("POST", "/secret/delete/app", "", None, "secret.write", "path:secret/delete/app?namespace=", "destructive"),
        ("POST", "/auth/token/revoke-accessor", "", None, "secret.write", "path:auth/token/revoke-accessor?namespace=", "destructive"),
        ("POST", "/transit/encrypt/orders", "", None, "secret.write", "path:transit/encrypt/orders?namespace=", "write"),
        ("POST", "/pki/issue/web", "", None, "secret.write", "path:pki/issue/web?namespace=", "write"),
        ("GET", "/database/creds/readonly", "", None, "raw:read", "path:database/creds/readonly?namespace=", "read"),
        ("PUT", "/sys/policies/acl/dev", "", None, "secret.write", "path:sys/policies/acl/dev?namespace=", "write"),
        ("POST", "/sys/leases/lookup", "", None, "secret.write", "path:sys/leases/lookup?namespace=", "write"),
        ("GET", "/sys/auth", "", None, "raw:read", "path:sys/auth?namespace=", "read"),
        ("POST", "/sys/mounts/kv2", "", None, "secret.write", "path:sys/mounts/kv2?namespace=", "write"),
        ("POST", "/auth/approle/login", "", None, "secret.write", "path:auth/approle/login?namespace=", "write"),
        # The namespace header, as Vault canonicalizes it, travels with the path.
        ("GET", "/secret/data/app", "", "team", "raw:read", "path:secret/data/app?namespace=team", "read"),
        ("GET", "/secret/data/app", "", "/team/child/", "raw:read", "path:secret/data/app?namespace=team/child", "read"),
        ("GET", "/team/secret/data/app", "", None, "raw:read", "path:team/secret/data/app?namespace=", "read"),
        ("GET", "/secret/data/app", "", "", "raw:read", "path:secret/data/app?namespace=", "read"),
    ],
)
def test_routing(method, path, query, ns, action, resource, effect):
    o = match(method, path, query, ns)
    assert o is not None and o.unresolved is None, o
    assert (o.action, o.resource, o.effect) == (action, resource, effect)
    assert find_action(Vault(), action) is not None
    # The resource is one the vault check parses, for the action.
    t = parse_target(action, parse_resource(resource))
    assert t.kind == "path" and t.namespace is not None


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/auth/token/lookup-self"),
        ("POST", "/auth/token/lookup-self"),
        ("POST", "/sys/capabilities-self"),
        ("GET", "/sys/internal/ui/resultant-acl"),
        ("GET", "/sys/health"),
        ("GET", "/sys/seal-status"),
    ],
)
def test_the_agents_own_token_and_health_check_nothing(method, path):
    o = match(method, path)
    assert o is not None and o.action is None and o.unresolved is None and o.effect == "read", o


@pytest.mark.parametrize(
    ("method", "path", "query", "why"),
    [
        # The calling token is the agent's.
        ("GET", "/cubbyhole/notes", "", "calling token"),
        ("POST", "/cubbyhole/notes", "", "calling token"),
        ("GET", "/cubbyhole", "list=true", "calling token"),
        ("POST", "/auth/token/revoke-self", "", "calling token"),
        ("POST", "/auth/token/renew-self", "", "calling token"),
        ("POST", "/identity/mfa/method/totp/generate", "", "calling token"),
        ("GET", "/sys/mfa/method/totp/corp/generate", "", "calling token"),
        ("GET", "/identity/oidc/token/ci", "", "calling token"),
        ("GET", "/identity/oidc/provider/corp/authorize", "", "calling token"),
        ("POST", "/sys/control-group/authorize", "", "calling token"),
        # A leading namespace in the path does not hide them.
        ("GET", "/team/cubbyhole/notes", "", "calling token"),
        ("POST", "/team/child/auth/token/create", "", "inherits"),
        ("POST", "/team/sys/seal", "", "sudo"),
        # Tokens minted with the agent's policies.
        ("POST", "/auth/token/create", "", "inherits"),
        ("POST", "/auth/token/create-orphan", "", "inherits"),
        ("POST", "/auth/token/create/ci", "", "inherits"),
        # sudo besides the capability.
        ("POST", "/sys/seal", "", "sudo"),
        ("POST", "/sys/step-down", "", "sudo"),
        ("POST", "/sys/auth/github", "", "sudo"),
        ("GET", "/sys/auth/github/tune", "", "sudo"),
        ("POST", "/sys/audit/file", "", "sudo"),
        ("GET", "/sys/raw/core/mounts", "", "sudo"),
        ("POST", "/sys/remount", "", "sudo"),
        ("POST", "/sys/leases/revoke-prefix/aws", "", "sudo"),
        ("GET", "/sys/leases/lookup/aws", "list=true", "sudo"),
        ("GET", "/sys/leases/lookup", "list=true", "sudo"),
        ("GET", "/sys/config/ui/headers", "list=true", "sudo"),
        ("PUT", "/sys/plugins/catalog/secret/kv", "", "sudo"),
        ("GET", "/auth/token/accessors", "list=true", "sudo"),
        ("POST", "/auth/token/revoke-orphan", "", "sudo"),
        ("DELETE", "/pki/root", "", "sudo"),
        ("POST", "/pki_int/root/sign-self-issued", "", "sudo"),
        # Operator operations.
        ("PUT", "/sys/unseal", "", "operator"),
        ("PUT", "/sys/init", "", "operator"),
        ("PUT", "/sys/rekey/update", "", "operator"),
        ("POST", "/sys/generate-root/attempt", "", "operator"),
        ("POST", "/sys/replication/performance/primary/enable", "", "operator"),
        ("PUT", "/sys/license/status", "", "operator"),
    ],
)
def test_refused_paths(method, path, query, why):
    o = match(method, path, query)
    assert o is not None and o.unresolved is not None and o.resource is None, o
    assert why in o.unresolved


@pytest.mark.parametrize(
    ("method", "path", "query", "ns", "why"),
    [
        ("GET", "/secret/data/app/", "", None, "trailing slash"),
        ("POST", "/secret/data/app/", "", None, "trailing slash"),
        ("GET", "//secret/data/app", "", None, "empty segment"),
        ("GET", "/secret/./data/app", "", None, "not a Vault path"),
        ("GET", "/secret/%2e%2e/sys/raw", "", None, "not a Vault path"),
        ("GET", "/secret/data/a%20b", "", None, "not a Vault path"),
        ("GET", "/secret/data/*", "", None, "not a Vault path"),
        ("GET", "/secret/metadata/app", "list=yes", None, "boolean"),
        ("GET", "/secret/metadata/app", "list=true&list=false", None, "more than once"),
        ("GET", "/secret/metadata/app", "list=true;x=1", None, "cannot be read"),
        ("GET", "/secret/metadata/app", "list=%zz", None, "cannot be read"),
        ("GET", "/secret/data/app", "", "te am", "X-Vault-Namespace"),
        ("GET", "/secret/data/app", "", "team//", "X-Vault-Namespace"),
        ("GET", "/secret/data/app", "", "a,b", "X-Vault-Namespace"),
        ("GET", "/secret/data/app", "", "../root", "X-Vault-Namespace"),
    ],
)
def test_paths_toolpass_cannot_read_exactly_are_refused(method, path, query, ns, why):
    o = match(method, path, query, ns)
    assert o is not None and o.unresolved is not None, o
    assert why in o.unresolved


def test_other_methods_are_not_routed():
    # A write with no route is refused by the hook; OPTIONS answers CORS headers only.
    assert match("OPTIONS", "/secret/data/app") is None
    assert match("TRACE", "/secret/data/app") is None


def test_a_secret_named_like_a_pki_root_is_refused_only_on_delete():
    assert match("DELETE", "/secret/data/root").unresolved is not None
    o = match("GET", "/secret/data/root")
    assert o is not None and o.unresolved is None and o.resource == "path:secret/data/root?namespace="


# -- the resources, answered by the vault check ------------------------------------


@pytest.mark.parametrize(
    ("resource", "namespace"),
    [
        ("path:secret/data/x", None),
        ("path:secret/data/x?namespace=", ""),
        ("path:secret/data/x?namespace=team/", "team"),
        ("path:secret/data/x?namespace=team/child", "team/child"),
    ],
)
def test_a_path_may_name_its_namespace(resource, namespace):
    assert parse_target("raw:read", parse_resource(resource)).namespace == namespace


@pytest.mark.parametrize(
    "resource",
    [
        "kv:secret/x?namespace=team",  # kv: resolves its mount in the connection's namespace
        "path:secret/data/x?v=1",
        "path:secret/data/x?namespace=team&v=1",
        "path:secret/data/x?namespace=a%20b",
        "path:secret/data/x?namespace=a/../b",
        "path:secret/data/x?namespace=a//b",
        "path:secret/data/x?namespace=*",
    ],
)
def test_other_queries_and_bad_namespaces_are_rejected(resource):
    with pytest.raises(ToolpassError):
        parse_target("raw:read" if resource.startswith("path:") else "secret.read", parse_resource(resource))


def observed(method: str, path: str, query: str = "", ns: str | None = None) -> tuple[str, str]:
    o = match(method, path, query, ns)
    assert o is not None and o.unresolved is None and o.action is not None and o.resource is not None, o
    return o.action, o.resource


def test_routed_requests_are_checked_by_the_vault_check(env: Env):
    _, _, c = env.setup()
    cases = [
        (("GET", "/secret/data/dev/app"), Code.ALLOWED),
        (("GET", "/secret/data/dev/locked"), Code.DENIED),
        (("POST", "/secret/data/dev/app"), Code.ALLOWED),
        (("POST", "/secret/data/dev/half"), Code.UNSUPPORTED),  # update without create: depends on whether it exists
        (("GET", "/secret/metadata/dev", "list=true"), Code.ALLOWED),
        (("DELETE", "/secret/metadata/dev/app"), Code.ALLOWED),
        (("PATCH", "/secret/data/dev/app"), Code.DENIED),
        (("GET", "/secret/data/prod/db"), Code.DENIED),
        (("GET", "/team/secrets/data/x"), Code.ALLOWED),
    ]
    for args, code in cases:
        action, resource = observed(*args)
        d = check(c, dana, action, resource)
        assert d.code == code, (args, d)


def test_a_namespace_header_against_a_root_connection_is_unknown(env: Env):
    # Vault Community ignores the header, Enterprise applies it: a root connection cannot tell.
    _, _, c = env.setup()
    d = check(c, ops, *observed("GET", "/secret/data/app", ns="team"))
    assert d.code == Code.UNSUPPORTED and "namespace team/" in d.text
    # Naming the child namespace in the path is the same request for both.
    d = check(c, ops, *observed("GET", "/team/secret/data/app"))
    assert d.code == Code.DENIED  # ops's policies grant secret/*, not team/secret/*


def test_requests_into_a_namespaced_connection(env: Env):
    _, _, c = env.setup_values({"namespace": "team/"}, "")
    # The header or the path, the same namespace: ops's secret/* applies.
    assert check(c, ops, *observed("GET", "/secret/data/app", ns="team")).code == Code.ALLOWED
    assert check(c, ops, *observed("GET", "/team/secret/data/app")).code == Code.ALLOWED
    assert check(c, ops, *observed("GET", "/secret/data/prod/db", ns="team/")).code == Code.DENIED
    # A child namespace is evaluated with the child's name in front, as Vault applies a parent's policies.
    assert check(c, ops, *observed("GET", "/secret/data/app", ns="team/child")).code == Code.DENIED
    # The root namespace, or a sibling, is outside the connection: its policies live elsewhere.
    for args in (("GET", "/secret/data/app"), ("GET", "/other/secret/data/app")):
        d = check(c, ops, *observed(*args))
        assert d.code == Code.UNSUPPORTED and "outside the connection's namespace team/" in d.text, args
    d = check(c, ops, *observed("GET", "/secret/data/app", ns="other"))
    assert d.code == Code.UNSUPPORTED

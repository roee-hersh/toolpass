"""The argocd route table against Argo CD's API description, its refusals,
and its resources answered by the argocd check itself."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from typing import Any

import pytest

from tests import harness as itest
from tests.harness.routes import check_routes_against_spec
from tests.integrations.argocd.test_argocd import Clock, check, default_cluster, dev, sre
from toolpass.core.catalog import parse_resource
from toolpass.core.context import background
from toolpass.core.decision import Code
from toolpass.core.integration import Connection, Settings, User, find_action
from toolpass.core.routes import Request
from toolpass.integrations import kubernetes
from toolpass.integrations.argocd import INTEGRATION, Integration
from toolpass.integrations.argocd.actions import build_request
from toolpass.integrations.argocd.routes import ROUTES

FINE = "project=shop&group=apps&kind=Deployment&namespace=shop&resourceName=web"


def req(method: str, path: str, query: str = "", body: Any = None, ct: str = "application/json") -> Request:
    data = body if isinstance(body, bytes) or body is None else json.dumps(body).encode()
    return Request("https", "argocd.example", 443, method, path, query, {"content-type": ct}, data)


def match(method: str, path: str, query: str = "", body: Any = None, ct: str = "application/json"):
    return ROUTES.match(req(method, path, query, body, ct))


def test_routes_match_the_api_description():
    # Every write the API describes under /api/v1 is routed (or refused by its route).
    assert check_routes_against_spec(ROUTES, "argocd", host="argocd.example") == []


def test_every_route_names_a_known_action():
    for r in ROUTES.routes:
        assert r.action is None or find_action(INTEGRATION, r.action) is not None, r


def test_endpoint_is_the_url_setting():
    assert ROUTES.endpoints(Settings("a", "argocd", {"url": "https://argocd.example"})) == [("argocd.example", "")]
    assert ROUTES.endpoints(Settings("a", "argocd", {})) == []


A = "/api/v1/applications/web"


@pytest.mark.parametrize(
    ("method", "path", "query", "body", "action", "resource", "effect"),
    [
        ("GET", A, "project=shop", None, "app.get", "applications:shop/web", "read"),
        ("GET", A, "projects=shop&refresh=hard", None, "app.get", "applications:shop/web", "read"),
        ("GET", A, "project=shop&appNamespace=team", None, "app.get", "applications:shop/team/web", "read"),
        ("GET", A + "/resource-tree", "project=shop", None, "app.get", "applications:shop/web", "read"),
        ("GET", A + "/manifests", "project=shop&revision=main", None, "app.get", "applications:shop/web", "read"),
        ("GET", A + "/resource", FINE, None, "app.get", "applications:shop/web", "read"),
        ("GET", A + "/revisions/abc/metadata", "project=shop", None, "app.get", "applications:shop/web", "read"),
        ("GET", A + "/links", "project=shop&namespace=team", None, "app.get", "applications:shop/team/web", "read"),
        ("GET", "/api/v1/stream/applications/web/resource-tree", "project=shop", None, "app.get", "applications:shop/web", "read"),
        ("GET", A + "/logs", "project=shop&podName=web-1&namespace=shop", None, "logs.get", "logs:shop/web", "read"),
        ("GET", A + "/pods/web-1/logs", "project=shop", None, "logs.get", "logs:shop/web", "read"),
        ("POST", "/api/v1/applications", "", {"metadata": {"name": "web"}, "spec": {"project": "shop"}}, "app.create", "applications:shop/web", "write"),
        (
            "POST",
            "/api/v1/applications",
            "validate=false",
            {"metadata": {"name": "web", "namespace": "team"}, "spec": {"project": "shop"}},
            "app.create",
            "applications:shop/team/web",
            "write",
        ),
        ("PUT", A, "project=shop", {"metadata": {"name": "web"}, "spec": {"project": "shop"}}, "app.update", "applications:shop/web", "write"),
        ("PUT", A + "/spec", "project=shop", {"project": "shop", "source": {}}, "app.update", "applications:shop/web", "write"),
        ("PATCH", A, "", {"project": "shop", "patch": '{"spec":{"source":{"targetRevision":"v2"}}}'}, "app.update", "applications:shop/web", "write"),
        ("DELETE", A, "project=shop&cascade=true", None, "app.delete", "applications:shop/web", "destructive"),
        ("POST", A + "/sync", "", {"project": "shop", "prune": True, "dryRun": False}, "app.sync", "applications:shop/web", "write"),
        ("POST", A + "/sync", "", {"project": "shop", "appNamespace": "team", "revision": ""}, "app.sync", "applications:shop/team/web", "write"),
        ("POST", A + "/rollback", "", {"project": "shop", "id": 3}, "app.rollback", "applications:shop/web", "write"),
        ("DELETE", A + "/operation", "project=shop", None, "app.sync", "applications:shop/web", "write"),
        ("POST", A + "/resource", FINE, b'"{}"', "app.update/apps/Deployment/shop/web", "applications:shop/web", "write"),
        ("DELETE", A + "/resource", FINE, None, "app.delete/apps/Deployment/shop/web", "applications:shop/web", "destructive"),
        (
            "DELETE",
            A + "/resource",
            "project=shop&kind=ConfigMap&namespace=shop&resourceName=cfg",
            None,
            "app.delete//ConfigMap/shop/cfg",
            "applications:shop/web",
            "destructive",
        ),
        ("POST", A + "/resource/actions", FINE, b'"restart"', "app.action/apps/Deployment/restart", "applications:shop/web", "write"),
        (
            "POST",
            A + "/resource/actions/v2",
            "",
            {"project": "shop", "group": "apps", "kind": "Deployment", "action": "restart", "resourceName": "web"},
            "app.action/apps/Deployment/restart",
            "applications:shop/web",
            "write",
        ),
        (
            "POST",
            "/api/v1/applicationsets",
            "",
            {"metadata": {"name": "fleet"}, "spec": {"template": {"spec": {"project": "shop"}}}},
            "appset.create",
            "applicationsets:shop/fleet",
            "write",
        ),
        ("GET", "/api/v1/projects/shop", "", None, "project.get", "projects:shop", "read"),
        ("GET", "/api/v1/projects/shop/syncwindows", "", None, "project.get", "projects:shop", "read"),
        ("POST", "/api/v1/projects", "", {"project": {"metadata": {"name": "shop"}}}, "project.create", "projects:shop", "write"),
        ("DELETE", "/api/v1/projects/shop", "", None, "project.delete", "projects:shop", "destructive"),
        ("POST", "/api/v1/projects/shop/roles/ci/token", "", {"project": "other"}, "project.update", "projects:shop", "write"),
        ("DELETE", "/api/v1/projects/shop/roles/ci/token/1700000000", "", None, "project.update", "projects:shop", "destructive"),
        ("POST", "/api/v1/clusters", "", {"server": "https://k8s.example", "name": "eu"}, "cluster.create", "clusters:https://k8s.example", "write"),
        ("POST", "/api/v1/clusters", "", {"server": "https://k8s.example", "project": "shop"}, "cluster.create", "clusters:shop/https://k8s.example", "write"),
        ("POST", "/api/v1/repositories", "", {"repo": "https://github.com/acme/app"}, "repo.create", "repositories:https://github.com/acme/app", "write"),
        (
            "POST",
            "/api/v1/write-repositories",
            "",
            {"repo": "https://github.com/acme/app", "project": "shop"},
            "writerepo.create",
            "write_repositories:shop/https://github.com/acme/app",
            "write",
        ),
        ("POST", "/api/v1/repocreds", "", {"url": "https://github.com/acme"}, "repo.create", "repositories:https://github.com/acme", "write"),
        ("PUT", "/api/v1/repocreds/https%3A%2F%2Fgithub.com%2Facme", "", {"url": "x"}, "repo.update", "repositories:https://github.com/acme", "write"),
        (
            "DELETE",
            "/api/v1/write-repocreds/https%3A%2F%2Fgithub.com%2Facme",
            "",
            None,
            "writerepo.delete",
            "write_repositories:https://github.com/acme",
            "destructive",
        ),
        ("GET", "/api/v1/account/ci-bot", "", None, "account.get", "accounts:ci-bot", "read"),
        ("POST", "/api/v1/account/ci-bot/token", "", {"name": "other"}, "account.update", "accounts:ci-bot", "write"),
    ],
)
def test_routing(method, path, query, body, action, resource, effect):
    o = match(method, path, query, body)
    assert o is not None and o.unresolved is None, o
    assert (o.action, o.resource, o.effect) == (action, resource, effect)
    assert find_action(INTEGRATION, action) is not None, action
    # The resource is one the argocd check parses, for the action.
    build_request(action, parse_resource(resource))


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/version"),
        ("GET", "/api/v1/settings"),
        ("GET", "/api/v1/session/userinfo"),
        ("POST", "/api/v1/session"),
        ("GET", "/api/v1/account/can-i/applications/get/shop%2Fweb"),
        ("GET", "/api/v1/notifications/triggers"),
    ],
)
def test_the_agents_own_identity_and_public_settings_check_nothing(method, path):
    o = match(method, path, "", {} if method == "POST" else None)
    assert o is not None and o.action is None and o.unresolved is None and o.effect == "read", o


@pytest.mark.parametrize(
    ("method", "path", "query", "body", "why"),
    [
        # The project must be named, once, and be the project the change lands in.
        ("GET", A, "", None, "name the application's project"),
        ("GET", A, "project=shop&project=ops", None, "one project"),
        ("GET", A, "project=shop&projects=shop", None, "once"),
        ("DELETE", A, "projects=shop", None, "name the application's project"),  # only a Get reads projects
        ("POST", A + "/sync", "project=shop", {"prune": True}, "name the application's project"),  # the body, not the query
        ("PUT", A, "project=shop", {"spec": {"project": "ops"}}, "both projects"),
        ("PUT", A, "", {"spec": {"project": "shop"}}, "both projects"),
        ("PUT", A + "/spec", "project=shop", {"project": "ops"}, "both projects"),
        ("PATCH", A, "", {"project": "shop", "patch": '{"spec":{"project":"ops"}}'}, "project"),
        ("PATCH", A, "", {"project": "shop", "patch": '[{"op":"replace","path":"/spec/project","value":"ops"}]'}, "project"),
        ("PATCH", A, "", {"project": "shop", "patch": '{"spec":{"Project":"ops"}}'}, "project"),
        ("PATCH", A, "", {"project": "shop", "patch": '{"spec":{"pro\\u006aect":"ops"}}'}, "project"),
        ("PATCH", A, "", {"project": "shop", "patch": '{"spec":{"project":"ops"},"spec":{"source":{}}}'}, "project"),
        # Argo CD updates the application the patched metadata names: another one.
        ("PATCH", A, "", {"project": "shop", "patch": '{"metadata":{"name":"billing"}}'}, "metadata"),
        ("PATCH", A, "", {"project": "shop", "patch": '[{"op":"replace","path":"/metadata/namespace","value":"team"}]'}, "metadata"),
        ("PATCH", A, "", {"project": "shop", "patch": "not json"}, "not JSON"),
        # Bodies read the way Go's decoder reads them, or refused.
        ("POST", A + "/sync", "", {"Project": "shop"}, "write it 'project'"),
        ("POST", A + "/sync", "", b'{"project": "dev", "project": "shop"}', "more than once"),
        ("POST", A + "/sync", "", b'{"project": "dev", "PROJECT": "shop"}', "more than once"),
        ("POST", A + "/sync", "", b'{"project": "shop"} trailing', "not a JSON document"),
        ("POST", A + "/sync", "", {"project": 3}, "must be a string"),
        ("POST", A + "/sync", "", [], "expected shape"),
        # A second action: override, update.
        ("POST", A + "/sync", "", {"project": "shop", "revision": "abc123"}, "override"),
        ("POST", A + "/sync", "", {"project": "shop", "revisions": ["abc"]}, "override"),
        ("POST", A + "/sync", "", {"project": "shop", "manifests": ["kind: Pod"]}, "override"),
        ("POST", "/api/v1/applications", "upsert=true", {"metadata": {"name": "web"}, "spec": {"project": "shop"}}, "upsert"),
        ("POST", "/api/v1/projects", "", {"project": {"metadata": {"name": "shop"}}, "upsert": True}, "upsert"),
        ("POST", "/api/v1/clusters", "upsert=true", {"server": "https://k8s.example"}, "upsert"),
        # Names toolpass checks.
        ("GET", "/api/v1/applications/Web", "project=shop", None, "not a valid name"),
        ("GET", A, "project=shop%2Fother", None, "not a valid name"),
        ("GET", A, "project=shop&appNamespace=a/b", None, "not a valid name"),
        ("GET", "/api/v1/applications/w%2Fx", "project=shop", None, "not a valid name"),
        ("POST", A + "/resource", "project=shop&kind=Deployment&namespace=shop", b'"{}"', "exactly"),
        ("POST", A + "/resource/actions", FINE, b'{"action": "restart"}', "JSON string"),
        ("POST", A + "/resource/actions", FINE, b'"re start"', "exactly"),
        ("GET", A, "project=shop;x=1", None, "cannot be read"),
        ("POST", "/api/v1/repocreds", "", {"url": "has space"}, "URL"),
        # Listings, objects whose project Argo CD looks up, and the rest.
        ("GET", "/api/v1/applications", "project=shop&name=web", None, "listing"),
        ("GET", "/api/v1/stream/applications", "", None, "listing"),
        ("GET", "/api/v1/projects", "", None, "listing"),
        ("GET", "/api/v1/clusters", "", None, "listing"),
        ("GET", "/api/v1/applicationsets/fleet", "", None, "project"),
        ("DELETE", "/api/v1/applicationsets/fleet", "", None, "project"),
        ("GET", "/api/v1/clusters/https%3A%2F%2Fk8s.example", "", None, "project"),
        ("DELETE", "/api/v1/clusters/https%3A%2F%2Fk8s.example", "", None, "project"),
        ("POST", "/api/v1/clusters/https%3A%2F%2Fk8s.example/rotate-auth", "", {}, "project"),
        ("PUT", "/api/v1/repositories/https%3A%2F%2Fgithub.com%2Facme", "", {}, "project"),
        ("DELETE", "/api/v1/repositories/https%3A%2F%2Fgithub.com%2Facme", "", None, "project"),
        ("POST", "/api/v1/repositories/https%3A%2F%2Fgithub.com%2Facme/validate", "", {}, "project"),
        ("PUT", "/api/v1/projects/shop", "", {"project": {}}, "clusters and repositories"),
        ("POST", "/api/v1/certificates", "", {}, "empty object"),
        ("DELETE", "/api/v1/gpgkeys", "keyID=abc", None, "empty object"),
        ("PUT", "/api/v1/account/password", "", {"newPassword": "x"}, "agent's"),
        ("DELETE", "/api/v1/session", "", None, "agent's"),
        ("POST", "/api/v1/applications/manifestsWithFiles", "", {}, "streamed"),
        ("GET", "/api/v1/somethingnew", "", None, "no route"),
        ("POST", "/api/webhook", "", {}, "no route"),
        ("GET", "/terminal", "pod=web-1&appName=web&projectName=shop", None, "exec"),
        ("GET", "/extensions/metrics/api", "", None, "extension"),
    ],
)
def test_refused(method, path, query, body, why):
    o = match(method, path, query, body)
    assert o is not None and o.unresolved is not None and o.resource is None, o
    assert why in o.unresolved, o.unresolved


def test_a_body_that_is_not_json_is_refused():
    o = match("POST", A + "/sync", "", b"project=shop", ct="application/x-www-form-urlencoded")
    assert o is not None and o.unresolved is not None and "JSON" in o.unresolved


def test_grpc_web_calls_are_not_routed():
    # The argocd CLI's --grpc-web calls are POSTs with no route: the hook refuses them as writes.
    assert match("POST", "/application.ApplicationService/Sync", "", b"\x00") is None


# -- the resources, answered by the argocd check -----------------------------------


@pytest.fixture
def connect() -> Iterator[Callable[[], Connection]]:
    servers: list[itest.Server] = []

    def make() -> Connection:
        srv = itest.Server()
        servers.append(srv)
        cl = default_cluster()
        assert cl.rbac_cm is not None
        cl.rbac_cm["policy.csv"] += "\np, role:viewer, applications, get, shop/web, allow\ng, viewers, role:viewer\n"
        cl.install(srv)
        deps, _ = itest.deps(srv, now=Clock())
        ks = itest.settings("k8s", "kubernetes", {"url": srv.url, "username_template": "{email}"}, {"credential": itest.literal("k8s")})
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
                "url": "https://argocd.example",
            },
        )
        return Integration().new(background(), s, deps)

    yield make
    for srv in servers:
        srv.close()


def observed(method: str, path: str, query: str = "", body: Any = None) -> tuple[str, str]:
    o = match(method, path, query, body)
    assert o is not None and o.unresolved is None and o.action is not None and o.resource is not None, o
    return o.action, o.resource


def test_routed_requests_are_checked_by_the_argocd_check(connect: Callable[[], Connection]):
    c = connect()
    cases = [
        (dev, ("GET", "/api/v1/applications/web", "project=dev"), Code.ALLOWED),
        (dev, ("GET", "/api/v1/applications/web", "project=prod"), Code.DENIED),
        (dev, ("POST", "/api/v1/applications/web/sync", "", {"project": "dev"}), Code.ALLOWED),
        (dev, ("DELETE", "/api/v1/applications/web", "project=dev"), Code.DENIED),
        (dev, ("GET", "/api/v1/applications/web/logs", "project=dev"), Code.ALLOWED),
        (sre, ("DELETE", "/api/v1/applications/web", "project=dev"), Code.ALLOWED),
        (sre, ("DELETE", "/api/v1/applications/web", "project=prod"), Code.DENIED),
        (sre, ("POST", "/api/v1/applications/web/resource/actions", FINE.replace("shop", "prod", 1), b'"restart"'), Code.ALLOWED),
        (sre, ("POST", "/api/v1/applications/web/resource", FINE.replace("shop", "prod", 1), b'"{}"'), Code.ALLOWED),
        (dev, ("POST", "/api/v1/applications/web/resource", FINE.replace("shop", "dev", 1), b'"{}"'), Code.DENIED),
        (sre, ("GET", "/api/v1/projects/dev"), Code.ALLOWED),
        (dev, ("GET", "/api/v1/projects/dev"), Code.DENIED),
    ]
    for user, args, code in cases:
        action, resource = observed(*args)
        d = check(c, user, action, resource)
        assert d.code == code, (user.email, args, action, resource, d)


def test_an_application_in_argocds_own_namespace_is_project_and_name(connect: Callable[[], Connection]):
    # Argo CD names an application in its own namespace shop/web (security.RBACName), so a rule
    # for shop/web covers it; in another namespace it is shop/team/web, which that rule does not.
    c = connect()
    viewer = User(email="v@example.com", groups=("viewers",))
    action, resource = observed("GET", "/api/v1/applications/web", "project=shop&appNamespace=argocd")
    assert resource == "applications:shop/argocd/web"
    assert check(c, viewer, action, resource).code == Code.ALLOWED
    assert check(c, viewer, *observed("GET", "/api/v1/applications/web", "project=shop")).code == Code.ALLOWED
    assert check(c, viewer, *observed("GET", "/api/v1/applications/web", "project=shop&appNamespace=team")).code == Code.DENIED

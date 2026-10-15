"""The kubernetes route table against the Kubernetes API description, and its shape."""

from __future__ import annotations

import pytest

from tests.harness.routes import check_routes_against_spec
from toolpass.core.integration import find_action
from toolpass.core.routes import Request
from toolpass.integrations.kubernetes import Integration
from toolpass.integrations.kubernetes.routes import ROUTES


def req(method: str, path: str, query: str = "") -> Request:
    return Request("https", "k8s.example", 6443, method, path, query, {"content-type": "application/json"}, b"{}" if method != "GET" else None)


def test_routes_match_the_api_description():
    # Every write the API describes under /api/v1 and /apis is routed or listed.
    assert check_routes_against_spec(ROUTES, "kubernetes", host="k8s.example") == []


@pytest.mark.parametrize(
    ("method", "path", "query", "action", "resource", "effect"),
    [
        ("GET", "/api/v1/namespaces/shop/pods/web-1", "", "raw:get:pods", "namespace:shop?resource=pods&name=web-1", "read"),
        ("GET", "/api/v1/namespaces/shop/pods", "", "raw:list:pods", "namespace:shop?resource=pods", "read"),
        ("GET", "/api/v1/namespaces/shop/pods", "watch=true", "raw:watch:pods", "namespace:shop?resource=pods", "read"),
        ("GET", "/api/v1/namespaces/shop/pods/web-1/log", "", "raw:get:pods/log", "namespace:shop?resource=pods&name=web-1&subresource=log", "read"),
        ("POST", "/api/v1/namespaces/shop/pods/web-1/exec", "", "raw:create:pods/exec", "namespace:shop?resource=pods&name=web-1&subresource=exec", "write"),
        (
            "GET",
            "/api/v1/namespaces/shop/pods/web-1/exec",
            "command=sh",
            "raw:get:pods/exec",
            "namespace:shop?resource=pods&name=web-1&subresource=exec",
            "write",
        ),
        ("DELETE", "/api/v1/namespaces/shop/pods/web-1", "", "raw:delete:pods", "namespace:shop?resource=pods&name=web-1", "destructive"),
        ("DELETE", "/api/v1/namespaces/shop/pods", "", "raw:deletecollection:pods", "namespace:shop?resource=pods", "destructive"),
        ("POST", "/apis/apps/v1/namespaces/shop/deployments", "", "raw:create:deployments.apps", "namespace:shop?resource=deployments.apps", "write"),
        (
            "PATCH",
            "/apis/apps/v1/namespaces/shop/deployments/web/scale",
            "",
            "raw:patch:deployments.apps/scale",
            "namespace:shop?resource=deployments.apps&name=web&subresource=scale",
            "write",
        ),
        (
            "PUT",
            "/apis/apps/v1/namespaces/shop/deployments/web",
            "",
            "raw:update:deployments.apps",
            "namespace:shop?resource=deployments.apps&name=web",
            "write",
        ),
        ("GET", "/api/v1/nodes", "", "raw:list:nodes", "cluster?resource=nodes", "read"),
        ("DELETE", "/api/v1/namespaces/shop", "", "raw:delete:namespaces", "cluster?resource=namespaces&name=shop", "destructive"),
        (
            "PUT",
            "/api/v1/namespaces/shop/finalize",
            "",
            "raw:update:namespaces/finalize",
            "cluster?resource=namespaces&name=shop&subresource=finalize",
            "write",
        ),
        (
            "POST",
            "/apis/rbac.authorization.k8s.io/v1/clusterrolebindings",
            "",
            "raw:create:clusterrolebindings.rbac.authorization.k8s.io",
            "cluster?resource=clusterrolebindings.rbac.authorization.k8s.io",
            "write",
        ),
        ("POST", "/apis/acme.io/v1alpha1/namespaces/shop/widgets", "", "raw:create:widgets.acme.io", "namespace:shop?resource=widgets.acme.io", "write"),
    ],
)
def test_routing(method, path, query, action, resource, effect):
    o = ROUTES.match(req(method, path, query))
    assert o is not None and o.unresolved is None, o
    assert (o.action, o.resource, o.effect) == (action, resource, effect)
    # The action and resource are ones the Kubernetes integration checks.
    a = find_action(Integration(), action)
    assert a is not None, action


@pytest.mark.parametrize("path", ["/version", "/api", "/api/v1", "/apis", "/apis/apps/v1", "/openapi/v2", "/healthz"])
def test_discovery_checks_nothing(path):
    o = ROUTES.match(req("GET", path))
    assert o is not None and o.action is None and o.unresolved is None, o


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/v1/namespaces/shop/services/web/proxy/admin"),
        ("POST", "/api/v1/namespaces/shop/pods/web-1/proxy/x/y"),
        ("POST", "/api/v1/namespaces/Shop/pods"),  # not a valid namespace name
        ("POST", "/apis/Apps/v1/namespaces/shop/deployments"),
        ("DELETE", "/api/v1/namespaces/shop/pods/-bad-"),
    ],
)
def test_proxies_and_bad_names_are_unresolved(method, path):
    o = ROUTES.match(req(method, path))
    assert o is not None and o.unresolved, o

"""The datadog route table against the v1 and v2 API descriptions, its
shape, and the resources it builds against the integration's own check."""

from __future__ import annotations

import json
import time
from typing import Any

import pytest

from tests import harness as itest
from tests.harness.routes import check_routes_against_spec
from tests.harness.spec import SpecOptions, any_spec, spec_from_env
from tests.integrations.datadog.test_datadog import Fake, bob, dana, root
from toolpass.core.catalog import parse_resource
from toolpass.core.context import background
from toolpass.core.decision import Code
from toolpass.core.integration import find_action
from toolpass.core.routes import Request, RouteTable, fill, template_fields
from toolpass.core.secret import literal
from toolpass.integrations.datadog import INTEGRATION, Datadog
from toolpass.integrations.datadog.actions import parse_target
from toolpass.integrations.datadog.routes import ROUTES

JSON = "application/json"


def req(method: str, path: str, body: Any = None, ct: str = JSON, headers: dict[str, str] | None = None) -> Request:
    data = body if isinstance(body, bytes) or body is None else json.dumps(body).encode()
    return Request("https", "api.datadoghq.com", 443, method, path, "", {"content-type": ct, **(headers or {})}, data)


@pytest.mark.parametrize("version", ["v1", "v2"])
def test_routes_match_the_api_description(version):
    """Each description is held to the routes of its own version; the
    claims and the routes left out are the table's."""
    part = RouteTable("datadog", [r for r in ROUTES.routes if r.path.startswith(f"/api/{version}/")], claims=ROUTES.claims, unrouted=ROUTES.unrouted)
    assert check_routes_against_spec(part, f"datadog-{version}", host="api.datadoghq.com") == []


def test_every_route_names_a_known_action():
    for r in ROUTES.routes:
        assert r.action is None or find_action(INTEGRATION, r.action) is not None, r


def test_every_fixed_resource_parses():
    """Each route's resource, filled with a valid id, is one the check accepts."""
    for r in ROUTES.routes:
        if r.action is None or "{target}" in r.resource:
            continue
        resource = fill(r.resource, {f: "123" for f in template_fields(r.resource)})
        assert resource is not None
        parse_target(r.action, parse_resource(resource))


ROUTING = [
    # Monitors.
    ("GET", "/api/v1/monitor/123", None, "monitor.read", "monitor:123", "read"),
    ("PUT", "/api/v1/monitor/123", {"id": 123, "name": "x"}, "monitor.edit", "monitor:123", "write"),
    ("DELETE", "/api/v1/monitor/123", None, "monitor.edit", "monitor:123", "destructive"),
    ("POST", "/api/v1/monitor", {"name": "x"}, "raw:monitors_write", "org", "write"),
    ("GET", "/api/v1/monitor/search", None, "raw:monitors_read", "org", "read"),
    ("POST", "/api/v1/monitor/validate", {}, "raw:monitors_read", "org", "read"),
    ("PATCH", "/api/v2/monitor/policy/p1", {}, "raw:monitor_config_policy_write", "org", "write"),
    # A read: on the org, not as a change to the monitor.
    ("GET", "/api/v2/monitor/123/downtime_matches", None, "raw:monitors_downtime", "org", "read"),
    # Downtimes.
    ("POST", "/api/v1/downtime", {"monitor_id": 123, "scope": ["*"]}, "monitor.mute", "monitor:123", "write"),
    ("POST", "/api/v1/downtime", {"scope": ["env:prod"]}, "raw:monitors_downtime", "org", "write"),
    ("PUT", "/api/v1/downtime/9", {"id": 9, "monitor_id": None, "scope": ["env:prod"]}, "raw:monitors_downtime", "org", "write"),
    ("DELETE", "/api/v1/downtime/9", None, "raw:monitors_downtime", "org", "destructive"),
    (
        "POST",
        "/api/v2/downtime",
        {"data": {"type": "downtime", "attributes": {"scope": "*", "monitor_identifier": {"monitor_id": 123}}}},
        "monitor.mute",
        "monitor:123",
        "write",
    ),
    (
        "POST",
        "/api/v2/downtime",
        {"data": {"type": "downtime", "attributes": {"scope": "*", "monitor_identifier": {"monitor_tags": ["team:a"]}}}},
        "raw:monitors_downtime",
        "org",
        "write",
    ),
    ("PATCH", "/api/v2/downtime/d1", {"data": {"id": "d1", "type": "downtime", "attributes": {"message": "m"}}}, "raw:monitors_downtime", "org", "write"),
    # Dashboards.
    ("GET", "/api/v1/dashboard/abc-def-ghi", None, "dashboard.read", "dashboard:abc-def-ghi", "read"),
    ("PUT", "/api/v1/dashboard/abc-def-ghi", {"title": "t"}, "dashboard.edit", "dashboard:abc-def-ghi", "write"),
    ("DELETE", "/api/v1/dashboard", {"data": [{"type": "dashboard", "id": "abc-def-ghi"}]}, "dashboard.edit", "dashboard:abc-def-ghi", "destructive"),
    ("POST", "/api/v1/dashboard", {"title": "t"}, "raw:dashboards_write", "org", "write"),
    ("POST", "/api/v2/dashboard/lists/manual/7/dashboards", {}, "raw:dashboards_write", "org", "write"),
    # SLOs and notebooks.
    ("GET", "/api/v1/slo/abc123", None, "raw:slos_read", "slo:abc123", "read"),
    ("PUT", "/api/v1/slo/abc123", {"name": "x"}, "slo.edit", "slo:abc123", "write"),
    ("POST", "/api/v1/slo/bulk_delete", {"abc123": ["7d"]}, "slo.edit", "slo:abc123", "destructive"),
    ("GET", "/api/v1/slo/search", None, "raw:slos_read", "org", "read"),
    ("GET", "/api/v1/notebooks/100", None, "raw:notebooks_read", "notebook:100", "read"),
    ("DELETE", "/api/v1/notebooks/100", None, "notebook.edit", "notebook:100", "destructive"),
    # Restriction policies.
    ("GET", "/api/v2/restriction_policy/dashboard:abc-def-ghi", None, "dashboard.read", "dashboard:abc-def-ghi", "read"),
    (
        "POST",
        "/api/v2/restriction_policy/monitor%3A123",
        {"data": {"id": "monitor:123", "type": "restriction_policy", "attributes": {"bindings": []}}},
        "monitor.edit",
        "monitor:123",
        "write",
    ),
    ("DELETE", "/api/v2/restriction_policy/slo:abc123", None, "slo.edit", "slo:abc123", "destructive"),
    # Data.
    ("POST", "/api/v2/logs/events/search", {"filter": {"query": "*"}}, "logs.read", "org", "read"),
    ("POST", "/api/v1/logs-queries/list", {}, "logs.read", "org", "read"),
    ("POST", "/api/v2/query/timeseries", {}, "raw:timeseries_query", "org", "read"),
    ("GET", "/api/v1/events", None, "raw:events_read", "org", "read"),
    ("GET", "/api/v2/logs/archive_searches/as-1", None, "logs.read", "org", "read"),
    ("GET", "/api/v2/trace/abc123", None, "raw:apm_read", "org", "read"),
    ("GET", "/api/v2/pruned_trace/abc123", None, "raw:apm_read", "org", "read"),
    # Users, roles, keys.
    ("POST", "/api/v2/users", {"data": {"type": "users", "attributes": {"email": "a@b.c"}}}, "raw:user_access_invite", "org", "write"),
    ("PATCH", "/api/v2/users/u-1", {}, "users.manage", "org", "write"),
    ("POST", "/api/v2/roles/r-1/users", {}, "users.manage", "org", "write"),
    ("DELETE", "/api/v2/roles/r-1", None, "users.manage", "org", "destructive"),
    ("POST", "/api/v2/api_keys", {}, "apikeys.manage", "org", "write"),
    # Reads that return the keys themselves.
    ("GET", "/api/v1/api_key", None, "raw:api_keys_read", "org", "read"),
    ("GET", "/api/v1/api_key/abc", None, "raw:api_keys_read", "org", "read"),
    ("GET", "/api/v2/api_keys", None, "raw:api_keys_read", "org", "read"),
    ("GET", "/api/v2/api_keys/k-1", None, "raw:api_keys_read", "org", "read"),
    ("GET", "/api/v1/application_key", None, "raw:org_app_keys_read", "org", "read"),
    ("GET", "/api/v1/application_key/k-1", None, "raw:org_app_keys_read", "org", "read"),
    ("GET", "/api/v2/application_keys", None, "raw:org_app_keys_read", "org", "read"),
    ("GET", "/api/v2/application_keys/k-1", None, "raw:org_app_keys_read", "org", "read"),
    ("DELETE", "/api/v2/api_keys/k-1", None, "raw:api_keys_delete", "org", "destructive"),
    ("DELETE", "/api/v1/application_key/k-1", None, "raw:org_app_keys_write", "org", "destructive"),
    ("POST", "/api/v2/service_accounts", {"data": {"type": "users", "attributes": {"email": "sa@b.c"}}}, "raw:service_account_write", "org", "write"),
    # Configuration.
    ("POST", "/api/v2/logs/config/archives", {}, "raw:logs_write_archives", "org", "write"),
    ("PATCH", "/api/v2/metrics/m.x/tags", {}, "raw:metric_tags_write", "org", "write"),
    ("POST", "/api/v2/metrics/derived-metrics", {}, "raw:metric_tags_write", "org", "write"),
    ("PUT", "/api/v2/metrics/derived-metrics/m.x", {}, "raw:metric_tags_write", "org", "write"),
    ("DELETE", "/api/v2/metrics/derived-metrics/m.x", None, "raw:metric_tags_write", "org", "destructive"),
    ("PUT", "/api/v1/metrics/m.x", {}, "raw:metrics_metadata_write", "org", "write"),
    ("GET", "/api/v1/validate", None, None, "", "read"),
]


@pytest.mark.parametrize(("method", "path", "body", "action", "resource", "effect"), ROUTING)
def test_routing(method, path, body, action, resource, effect):
    o = ROUTES.match(req(method, path, body))
    assert o is not None
    assert o.unresolved is None, o.unresolved
    assert (o.action, o.resource, o.effect) == (action, resource, effect)
    if action is not None:
        parse_target(action, parse_resource(resource))


def test_sends_out_and_untrusted_reads():
    assert ROUTES.match(req("POST", "/api/v2/user_invitations", {})).sends_out
    assert ROUTES.match(req("POST", "/api/v2/logs/config/custom-destinations", {})).sends_out
    assert not ROUTES.match(req("POST", "/api/v1/monitor", {})).sends_out
    assert ROUTES.match(req("GET", "/api/v1/monitor/123")).untrusted_output
    assert not ROUTES.match(req("GET", "/api/v1/validate")).untrusted_output


def unresolved(method: str, path: str, body: Any, ct: str = JSON, headers: dict[str, str] | None = None) -> str:
    o = ROUTES.match(req(method, path, body, ct, headers))
    assert o is not None, path
    assert o.unresolved is not None, (path, body)
    return o.unresolved


@pytest.mark.parametrize(
    ("method", "path", "body", "why"),
    [
        # A second asset beside the path's.
        ("PUT", "/api/v1/monitor/123", {"id": 456, "name": "x"}, "another asset"),
        ("PUT", "/api/v1/dashboard/abc", {"id": "xyz"}, "another asset"),
        ("PUT", "/api/v1/notebooks/100", {"data": {"id": 200, "type": "notebooks"}}, "data.id"),
        ("PUT", "/api/v1/downtime/9", {"id": 10, "scope": ["*"]}, "another downtime"),
        ("PATCH", "/api/v2/downtime/d1", {"data": {"id": "d2", "type": "downtime", "attributes": {}}}, "another downtime"),
        ("POST", "/api/v2/restriction_policy/dashboard:abc", {"data": {"id": "dashboard:xyz", "attributes": {"bindings": []}}}, "data.id"),
        # Bulk requests: one asset per request.
        ("DELETE", "/api/v1/dashboard", {"data": [{"type": "dashboard", "id": "a"}, {"type": "dashboard", "id": "b"}]}, "one dashboard"),
        ("PATCH", "/api/v1/dashboard", {"data": []}, "one dashboard"),
        ("POST", "/api/v1/slo/bulk_delete", {"a": ["7d"], "b": ["7d"]}, "one SLO"),
        # Ids the check cannot take.
        ("GET", "/api/v1/monitor/1%2F2", None, "asset id"),
        ("POST", "/api/v1/downtime", {"monitor_id": 1.5}, "monitor_id"),
        ("POST", "/api/v1/downtime", {"monitor_id": True}, "monitor_id"),
        ("POST", "/api/v2/downtime", {"data": {"attributes": {"monitor_identifier": {"monitor_id": 1, "monitor_tags": []}}}}, "either"),
        # Assets whose restriction policies the integration does not read.
        ("POST", "/api/v2/restriction_policy/synthetics-test:abc", {"data": {"id": "synthetics-test:abc"}}, "monitors, dashboards"),
        ("GET", "/api/v2/restriction_policy/workflow:abc", None, "monitors, dashboards"),
        # Roles need a second permission.
        ("POST", "/api/v2/users", {"data": {"type": "users", "relationships": {"roles": {"data": [{"id": "r", "type": "roles"}]}}}}, "without roles"),
        ("POST", "/api/v1/user", {"handle": "a@b.c", "access_role": "adm"}, "without roles"),
        ("POST", "/api/v2/service_accounts", {"data": {"relationships": {"roles": {"data": [{"id": "r"}]}}}}, "without roles"),
    ],
)
def test_refusals(method, path, body, why):
    assert why in unresolved(method, path, body)


def test_bodies_are_read_strictly():
    body = {"monitor_id": 123}
    assert "Content-Type" in unresolved("POST", "/api/v1/downtime", body, ct="text/plain")
    assert "compressed" in unresolved("POST", "/api/v1/downtime", body, headers={"content-encoding": "gzip"})
    assert "twice" in unresolved("POST", "/api/v1/downtime", b'{"monitor_id": 1, "monitor_id": null}')
    assert "twice" in unresolved("DELETE", "/api/v1/dashboard", b'{"data": [{"type": "dashboard", "id": "a", "id": "b"}]}')
    assert "not JSON" in unresolved("POST", "/api/v1/downtime", b'{"monitor_id": Infinity}')
    assert "not valid JSON" in unresolved("POST", "/api/v1/downtime", b'{"monitor_id": ')


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("PATCH", "/api/v2/service_accounts/../application_keys/x"),
        ("PATCH", "/api/v2/service_accounts/sa/%2e%2e/application_keys/x"),
        ("GET", "/api/v1/monitor/123/%2E"),
        ("DELETE", "/api/v1/monitor/./123"),
    ],
)
def test_dot_segments_are_not_routed(method, path):
    o = ROUTES.match(req(method, path, {}))
    assert o is not None and o.route is None and o.unresolved is not None and ". or .." in o.unresolved


def test_writes_left_out():
    for method, path in (
        ("PUT", "/api/v1/synthetics/tests/api/abc"),  # restriction policies not read
        ("POST", "/api/v1/series"),  # intake
        ("POST", "/api/v1/application_key"),  # the agent's own key
        ("POST", "/api/v2/incidents"),
        ("POST", "/api/v1/dashboard/public"),
        ("PUT", "/api/v1/logs/config/pipelines/p1"),
    ):
        assert ROUTES.match(req(method, path, {})) is None, path
        assert ROUTES.is_unrouted(method, path) is not None, path


def test_endpoints_follow_the_site():
    def settings(url: str) -> Any:
        return type("S", (), {"get": lambda self, k: url})()

    assert ROUTES.endpoints(settings("")) == [("api.datadoghq.com", ""), ("app.datadoghq.com", ""), ("http-intake.logs.datadoghq.com", "")]
    assert ROUTES.endpoints(settings("https://api.datadoghq.eu")) == [("api.datadoghq.eu", ""), ("app.datadoghq.eu", ""), ("http-intake.logs.datadoghq.eu", "")]
    assert ROUTES.endpoints(settings("https://api.us3.datadoghq.com/")) == [
        ("api.us3.datadoghq.com", ""),
        ("us3.datadoghq.com", ""),
        ("http-intake.logs.us3.datadoghq.com", ""),
    ]
    assert ROUTES.endpoints(settings("https://dd-proxy.example.com/datadog")) == [("dd-proxy.example.com", "/datadog")]


def test_routed_resources_through_the_check():
    """What a request maps to is what the integration answers: dana holds the
    Standard role, bob Read Only, root Admin."""
    srv = itest.Server()
    try:
        srv.use_spec(any_spec(spec_from_env("datadog-v1"), spec_from_env("datadog-v2")), SpecOptions())
        f = Fake()
        srv.handle("GET", "/api/*", f.api)
        deps, _ = itest.deps(srv, now=time.time)
        s = itest.settings("dd", "datadog", {"url": srv.url}, {"api_key": literal(f.api_key), "credential": literal(f.app_key)})
        c = Datadog().new(background(), s, deps)

        def decide(user: Any, method: str, path: str, body: Any = None) -> Code | None:
            o = ROUTES.match(req(method, path, body))
            assert o is not None and o.unresolved is None and o.action is not None and o.resource is not None
            return itest.check(c, Datadog(), user, o.action, o.resource).code

        assert decide(dana, "PUT", "/api/v1/monitor/1", {"name": "x"}) == Code.ALLOWED
        assert decide(bob, "PUT", "/api/v1/monitor/1", {"name": "x"}) == Code.DENIED
        assert decide(dana, "PUT", "/api/v1/monitor/2", {"name": "x"}) == Code.DENIED  # restricted to a role dana lacks
        assert decide(bob, "GET", "/api/v1/monitor/1") == Code.ALLOWED
        assert decide(dana, "POST", "/api/v1/downtime", {"monitor_id": 1}) == Code.ALLOWED
        assert decide(bob, "POST", "/api/v1/downtime", {"scope": ["*"]}) == Code.DENIED
        assert decide(bob, "POST", "/api/v1/monitor", {"name": "x"}) == Code.DENIED
        assert decide(bob, "POST", "/api/v2/logs/events/search", {}) == Code.ALLOWED
        assert decide(dana, "GET", "/api/v1/slo/slo1") == Code.ALLOWED
        # The notebook's restriction policy names bob as its only viewer.
        assert decide(dana, "DELETE", "/api/v1/notebooks/100") == Code.DENIED
        assert decide(dana, "GET", "/api/v1/notebooks/100") == Code.DENIED
        assert decide(bob, "GET", "/api/v1/notebooks/100") == Code.ALLOWED
        assert decide(dana, "DELETE", "/api/v2/users/u-1") == Code.DENIED
        assert decide(root, "DELETE", "/api/v2/users/u-1") == Code.ALLOWED
        assert decide(root, "POST", "/api/v2/api_keys", {}) == Code.ALLOWED
        # Reading keys returns them: the read permission, which Read Only lacks.
        assert decide(dana, "GET", "/api/v1/api_key") == Code.ALLOWED
        assert decide(bob, "GET", "/api/v1/api_key") == Code.DENIED
        assert decide(bob, "GET", "/api/v2/application_keys/k-1") == Code.DENIED
        # Monitor 2 is restricted to a role dana lacks; reading its downtime matches is not a change to it.
        assert decide(dana, "GET", "/api/v2/monitor/2/downtime_matches") == Code.ALLOWED
        assert decide(bob, "GET", "/api/v2/monitor/1/downtime_matches") == Code.DENIED  # no monitors_downtime
    finally:
        srv.close()
    assert not f.errors, f.errors
    assert not srv.spec_errors, srv.spec_errors

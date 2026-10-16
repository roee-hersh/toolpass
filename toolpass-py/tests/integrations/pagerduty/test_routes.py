"""The pagerduty route table against the REST API description, its shape,
and the resources it builds against the integration's own check."""

from __future__ import annotations

import json
from typing import Any

import pytest

from tests import harness as itest
from tests.harness.routes import check_routes_against_spec
from tests.harness.spec import SpecOptions, spec_from_env
from tests.integrations.pagerduty.test_pagerduty import Fake, manager, observer, responder
from toolpass.core.catalog import parse_resource
from toolpass.core.context import background
from toolpass.core.decision import Code
from toolpass.core.integration import find_action
from toolpass.core.routes import Request, fill, template_fields
from toolpass.core.secret import literal
from toolpass.integrations.pagerduty import INTEGRATION, PagerDuty
from toolpass.integrations.pagerduty.actions import parse_target
from toolpass.integrations.pagerduty.routes import ROUTES

JSON = "application/json"


def req(method: str, path: str, body: Any = None, ct: str = JSON, headers: dict[str, str] | None = None, host: str = "api.pagerduty.com") -> Request:
    data = body if isinstance(body, bytes) or body is None else json.dumps(body).encode()
    return Request("https", host, 443, method, path, "", {"content-type": ct, **(headers or {})}, data)


def ref(i: str, typ: str = "team_reference") -> dict[str, str]:
    return {"id": i, "type": typ}


def test_routes_match_the_api_description():
    assert check_routes_against_spec(ROUTES, "pagerduty", host="api.pagerduty.com") == []


def test_every_route_names_a_known_action():
    for r in ROUTES.routes:
        assert r.action is None or find_action(INTEGRATION, r.action) is not None, r


def test_every_fixed_resource_parses():
    """Each route's resource, filled with a valid id, is one the check accepts."""
    for r in ROUTES.routes:
        if r.action is None or "{target}" in r.resource:
            continue
        resource = fill(r.resource, {f: "PABC123" for f in template_fields(r.resource)})
        assert resource is not None
        parse_target(r.action, parse_resource(resource))


ROUTING = [
    ("PUT", "/incidents/PINC1", {"incident": {"type": "incident_reference", "status": "acknowledged"}}, "incident.acknowledge", "incident:PINC1", "write"),
    (
        "PUT",
        "/incidents/PINC1",
        {"incident": {"type": "incident_reference", "status": "resolved", "resolution": "fixed"}},
        "incident.resolve",
        "incident:PINC1",
        "write",
    ),
    ("PUT", "/incidents/PINC1", {"incident": {"type": "incident_reference", "escalation_level": 2}}, "incident.reassign", "incident:PINC1", "write"),
    (
        "PUT",
        "/incidents/PINC1",
        {"incident": {"type": "incident", "assignments": [{"assignee": ref("PU1", "user_reference")}]}},
        "incident.reassign",
        "incident:PINC1",
        "write",
    ),
    (
        "PUT",
        "/incidents",
        {"incidents": [{"id": "PINC2", "type": "incident_reference", "status": "acknowledged"}]},
        "incident.acknowledge",
        "incident:PINC2",
        "write",
    ),
    ("PUT", "/incidents/PINC1/alerts/PAL1", {"alert": {"type": "alert", "status": "resolved"}}, "incident.resolve", "incident:PINC1", "write"),
    ("PUT", "/incidents/PINC1/alerts", {"alerts": [{"id": "PAL1", "type": "alert", "status": "resolved"}]}, "incident.resolve", "incident:PINC1", "write"),
    ("POST", "/incidents/PINC1/snooze", {"duration": 3600}, "incident.acknowledge", "incident:PINC1", "write"),
    ("POST", "/incidents/PINC1/responder_requests", {}, "incident.reassign", "incident:PINC1", "write"),
    (
        "POST",
        "/services",
        {"service": {"type": "service", "name": "x", "escalation_policy": ref("PEP1", "escalation_policy_reference")}},
        "escalation_policy.edit",
        "escalation_policy:PEP1",
        "write",
    ),
    ("PUT", "/services/PSVC1", {"service": {"type": "service", "name": "renamed"}}, "service.edit", "service:PSVC1", "write"),
    ("DELETE", "/services/PSVC1", None, "service.edit", "service:PSVC1", "destructive"),
    ("POST", "/services/PSVC1/integrations", {}, "service.edit", "service:PSVC1", "write"),
    ("PUT", "/event_orchestrations/services/PSVC2", {}, "service.edit", "service:PSVC2", "write"),
    (
        "POST",
        "/maintenance_windows",
        {"maintenance_window": {"type": "maintenance_window", "start_time": "a", "end_time": "b", "services": [ref("PSVC1", "service_reference")]}},
        "service.maintenance",
        "service:PSVC1",
        "write",
    ),
    (
        "POST",
        "/escalation_policies",
        {"escalation_policy": {"type": "escalation_policy", "name": "x", "escalation_rules": [], "teams": [ref("PTEAM1")]}},
        "team.manage",
        "team:PTEAM1",
        "write",
    ),
    (
        "POST",
        "/escalation_policies",
        {"escalation_policy": {"type": "escalation_policy", "name": "x", "escalation_rules": []}},
        "account.admin",
        "account",
        "write",
    ),
    (
        "PUT",
        "/escalation_policies/PEP1",
        {"escalation_policy": {"type": "escalation_policy", "name": "y"}},
        "escalation_policy.edit",
        "escalation_policy:PEP1",
        "write",
    ),
    ("POST", "/schedules", {"schedule": {"type": "schedule", "time_zone": "UTC", "teams": [ref("PTEAM2")]}}, "team.manage", "team:PTEAM2", "write"),
    ("PUT", "/schedules/PSCH1", {"schedule": {"type": "schedule", "time_zone": "UTC"}}, "schedule.edit", "schedule:PSCH1", "write"),
    ("POST", "/schedules/PSCH1/overrides", {"overrides": []}, "schedule.override", "schedule:PSCH1", "write"),
    ("DELETE", "/schedules/PSCH1/overrides/POV1", None, "schedule.override", "schedule:PSCH1", "destructive"),
    ("PUT", "/v3/schedules/PSCH1", {"schedule": {"name": "x", "time_zone": "UTC"}}, "schedule.edit", "schedule:PSCH1", "write"),
    ("DELETE", "/v3/schedules/PSCH1", None, "schedule.edit", "schedule:PSCH1", "destructive"),
    ("PUT", "/v3/schedules/PSCH1/rotations/PR1/events/PE1", {}, "schedule.edit", "schedule:PSCH1", "write"),
    ("POST", "/v3/schedules/PSCH1/overrides", {}, "schedule.override", "schedule:PSCH1", "write"),
    ("PUT", "/teams/PTEAM1/users/PU1", {"role": "manager"}, "team.manage", "team:PTEAM1", "write"),
    ("DELETE", "/teams/PTEAM1", None, "account.admin", "account", "destructive"),
    ("POST", "/teams/PTEAM1/change_tags", {"add": []}, "team.manage", "team:PTEAM1", "write"),
    ("POST", "/escalation_policies/PEP1/change_tags", {"add": []}, "escalation_policy.edit", "escalation_policy:PEP1", "write"),
    ("POST", "/users/PU1/change_tags", {"add": []}, "account.admin", "account", "write"),
    ("POST", "/users", {}, "account.admin", "account", "write"),
    ("POST", "/webhook_subscriptions", {}, "account.admin", "account", "write"),
    ("PUT", "/incidents/types/major", {}, "account.admin", "account", "write"),
    ("POST", "/analytics/raw/incidents", {}, None, "", "read"),
    ("POST", "/schedules/preview", {}, None, "", "read"),
]


@pytest.mark.parametrize(("method", "path", "body", "action", "resource", "effect"), ROUTING)
def test_routing(method, path, body, action, resource, effect):
    o = ROUTES.match(req(method, path, body))
    assert o is not None
    assert o.unresolved is None, o.unresolved
    assert (o.action, o.resource, o.effect) == (action, resource, effect)
    if action is not None:
        parse_target(action, parse_resource(resource))


def test_sends_out_and_reads():
    assert ROUTES.match(req("POST", "/incidents/PINC1/responder_requests", {})).sends_out
    assert ROUTES.match(req("POST", "/webhook_subscriptions", {})).sends_out
    assert ROUTES.match(req("POST", "/status_pages/PSP1/posts", {})).sends_out
    assert not ROUTES.match(req("POST", "/users", {})).sends_out
    assert ROUTES.match(req("GET", "/incidents/PINC1")) is None  # an unmapped read: allowed, untrusted


def unresolved(method: str, path: str, body: Any, ct: str = JSON, headers: dict[str, str] | None = None) -> str:
    o = ROUTES.match(req(method, path, body, ct, headers))
    assert o is not None, path
    assert o.unresolved is not None, (path, body)
    return o.unresolved


@pytest.mark.parametrize(
    ("method", "path", "body", "why"),
    [
        # Incident updates: one action, nothing else.
        ("PUT", "/incidents/PINC1", {"incident": {"type": "incident_reference", "status": "resolved", "escalation_level": 2}}, "separate actions"),
        ("PUT", "/incidents/PINC1", {"incident": {"type": "incident_reference", "status": "acknowledged", "priority": {}}}, "priority"),
        ("PUT", "/incidents/PINC1", {"incident": {"type": "incident_reference", "service": ref("PSVC2", "service_reference")}}, "service"),
        ("PUT", "/incidents/PINC1", {"incident": {"type": "incident_reference", "status": "triggered"}}, "acknowledged or resolved"),
        ("PUT", "/incidents/PINC1", {"incident": {"type": "incident_reference", "resolution": "x"}}, "status resolved"),
        ("PUT", "/incidents/PINC1", {"incident": {"type": "incident_reference"}}, "nothing"),
        ("PUT", "/incidents/PINC1", {"incident": {"status": "resolved"}}, "type"),
        ("PUT", "/incidents/PINC1", {"incident": {"type": "incident", "status": "resolved"}, "extra": 1}, "one object"),
        ("PUT", "/incidents/pinc1", {"incident": {"type": "incident", "status": "resolved"}}, "PagerDuty id"),
        # Bulk updates: one incident per request.
        (
            "PUT",
            "/incidents",
            {"incidents": [{"id": "PINC1", "type": "incident", "status": "resolved"}, {"id": "PINC2", "type": "incident", "status": "resolved"}]},
            "one incident",
        ),
        ("PUT", "/incidents", {"incidents": [{"type": "incident", "status": "resolved"}]}, "id"),
        # Alerts may only be resolved, never moved.
        (
            "PUT",
            "/incidents/PINC1/alerts/PAL1",
            {"alert": {"type": "alert", "status": "resolved", "incident": ref("PINC2", "incident_reference")}},
            "two incidents",
        ),
        ("PUT", "/incidents/PINC1/alerts/PAL1", {"alert": {"type": "alert", "status": "triggered"}}, "only resolve"),
        ("PUT", "/incidents/PINC1/alerts/PAL1", {"alert": {"id": "PAL2", "type": "alert", "status": "resolved"}}, "another alert"),
        ("PUT", "/incidents/PINC1/alerts", {"alerts": []}, "non-empty"),
        # A second target in the body.
        ("PUT", "/services/PSVC1", {"service": {"type": "service", "escalation_policy": ref("PEP2", "escalation_policy_reference")}}, "between teams"),
        ("PUT", "/services/PSVC1", {"service": {"type": "service", "id": "PSVC2"}}, "not the one in the path"),
        (
            "PUT",
            "/escalation_policies/PEP1",
            {"escalation_policy": {"type": "escalation_policy", "services": [ref("PSVC2", "service_reference")]}},
            "between teams",
        ),
        ("PUT", "/schedules/PSCH1", {"schedule": {"type": "schedule", "teams": [ref("PTEAM2")]}}, "between teams"),
        ("PUT", "/v3/schedules/PSCH1", {"schedule": {"time_zone": "UTC", "teams": [ref("POTHER")]}}, "between teams"),
        ("PUT", "/v3/schedules/PSCH1", {"schedule": {"id": "PSCH2", "time_zone": "UTC"}}, "not the one in the path"),
        ("PUT", "/v3/schedules/PSCH1", {"time_zone": "UTC"}, "one object under 'schedule'"),
        ("POST", "/services", {"service": {"type": "service", "escalation_policy": ref("PEP1"), "teams": [ref("PTEAM1")]}}, "teams"),
        ("POST", "/services", {"service": {"type": "service"}}, "escalation_policy"),
        ("POST", "/escalation_policies", {"escalation_policy": {"type": "escalation_policy", "teams": [ref("PTEAM1"), ref("PTEAM2")]}}, "exactly one"),
        ("POST", "/escalation_policies", {"escalation_policy": {"type": "escalation_policy", "services": [ref("PSVC1")]}}, "attaches"),
        ("POST", "/maintenance_windows", {"maintenance_window": {"services": [ref("PSVC1"), ref("PSVC2")]}}, "exactly one"),
        ("POST", "/extensions", {"extension": {"extension_objects": [ref("PSVC1", "service_reference"), ref("PSVC2", "service_reference")]}}, "exactly one"),
        ("POST", "/business_services/PBS1/change_tags", {"add": []}, "users, teams"),
    ],
)
def test_refusals(method, path, body, why):
    assert why in unresolved(method, path, body)


def test_bodies_are_read_strictly():
    ack = {"incident": {"type": "incident_reference", "status": "acknowledged"}}
    assert "Content-Type" in unresolved("PUT", "/incidents/PINC1", ack, ct="text/plain")
    assert "compressed" in unresolved("PUT", "/incidents/PINC1", ack, headers={"content-encoding": "gzip"})
    dup = b'{"incident": {"type": "incident_reference", "status": "acknowledged", "status": "resolved"}}'
    assert "twice" in unresolved("PUT", "/incidents/PINC1", dup)
    assert "not JSON" in unresolved("PUT", "/incidents/PINC1", b'{"incident": {"type": "incident", "escalation_level": NaN}}')
    assert "no body" in unresolved("PUT", "/incidents/PINC1", None)


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("DELETE", "/schedules/PSCHED1/overrides/%2e%2e"),
        ("PUT", "/incidents/PINC1/../PINC2"),
        ("PUT", "/services/PSVC1/%2E"),
        ("PUT", "/v3/schedules/./PSCH1"),
    ],
)
def test_dot_segments_are_not_routed(method, path):
    o = ROUTES.match(req(method, path, {}))
    assert o is not None and o.route is None and o.unresolved is not None and ". or .." in o.unresolved


def test_writes_without_an_action_are_left_out():
    for method, path in (("POST", "/incidents"), ("POST", "/incidents/PINC1/notes"), ("PUT", "/incidents/PINC1/merge"), ("PUT", "/session_configurations")):
        assert ROUTES.match(req(method, path, {})) is None, path
        assert ROUTES.is_unrouted(method, path) is not None, path


EVENTS_HOSTS = [("events.pagerduty.com", ""), ("events.eu.pagerduty.com", "")]


def test_endpoint_from_the_url_setting():
    def settings(url: str) -> Any:
        return type("S", (), {"get": lambda self, k: url})()

    assert ROUTES.endpoints(settings("")) == [("api.pagerduty.com", ""), *EVENTS_HOSTS]
    assert ROUTES.endpoints(settings("https://api.eu.pagerduty.com")) == [("api.eu.pagerduty.com", ""), *EVENTS_HOSTS]
    assert ROUTES.endpoints(settings("https://pd-proxy.example.com/pd")) == [("pd-proxy.example.com", "/pd")]


@pytest.mark.parametrize("host", ["events.pagerduty.com", "events.eu.pagerduty.com"])
@pytest.mark.parametrize(
    "path",
    ["/v2/enqueue", "/v2/change/enqueue", "/integration/0123456789abcdef0123456789abcdef/enqueue", "/generic/2010-04-15/create_event.json"],
)
def test_events_api_is_located_and_refused(host, path):
    """The Events API resolves and acknowledges incidents with a routing key
    alone: its host is the connection's, so the write is refused as unrouted
    rather than passing as an unknown host."""
    from toolpass._wire import Connections

    conns = Connections.from_mappings([{"id": "pd", "integration": "pagerduty"}])
    body = {"routing_key": "0123456789abcdef0123456789abcdef", "event_action": "resolve", "dedup_key": "x"}
    hit = conns.resolve(req("POST", path, body, host=host))
    assert hit is not None and hit[0].id == "pd"
    assert ROUTES.match(req("POST", path, body, host=host), hit[1]) is None
    assert ROUTES.is_unrouted("POST", path) is not None


def test_routed_resources_through_the_check():
    """What a request maps to is what the integration answers: a team
    manager on PTEAM1 and responder on PTEAM2, a Responder and a Manager."""
    srv = itest.Server()
    try:
        srv.use_spec(spec_from_env("pagerduty"), SpecOptions())
        f = Fake()
        srv.handle("GET", "/*", f.api)
        deps, _ = itest.deps(srv)
        s = itest.settings("pd", "pagerduty", {"url": srv.url}, {"credential": literal(f.key)})
        c = PagerDuty().new(background(), s, deps)

        def decide(user: Any, method: str, path: str, body: Any) -> Code:
            o = ROUTES.match(req(method, path, body))
            assert o is not None and o.unresolved is None and o.action is not None and o.resource is not None
            return itest.check(c, PagerDuty(), user, o.action, o.resource).code

        ack = {"incident": {"type": "incident_reference", "status": "acknowledged"}}
        assert decide(observer, "PUT", "/incidents/PINC2", ack) == Code.ALLOWED
        assert decide(observer, "PUT", "/services/PSVC2", {"service": {"type": "service", "name": "x"}}) == Code.DENIED
        assert decide(observer, "PUT", "/services/PSVC1", {"service": {"type": "service", "name": "x"}}) == Code.ALLOWED
        new_policy = {"escalation_policy": {"type": "escalation_policy", "name": "x", "escalation_rules": [], "teams": [ref("PTEAM1")]}}
        assert decide(observer, "POST", "/escalation_policies", new_policy) == Code.ALLOWED
        new_service = {"service": {"type": "service", "name": "x", "escalation_policy": ref("PEP2", "escalation_policy_reference")}}
        assert decide(observer, "POST", "/services", new_service) == Code.DENIED
        assert decide(responder, "PUT", "/incidents/PINC0", ack) == Code.ALLOWED
        assert decide(responder, "POST", "/schedules/PSCH1/overrides", {"overrides": []}) == Code.ALLOWED
        assert decide(responder, "DELETE", "/schedules/PSCH1", None) == Code.DENIED
        assert decide(manager, "POST", "/users", {}) == Code.DENIED  # account configuration needs an owner or admin
    finally:
        srv.close()
    assert not srv.spec_errors, srv.spec_errors

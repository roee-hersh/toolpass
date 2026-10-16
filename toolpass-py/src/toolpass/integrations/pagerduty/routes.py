"""PagerDuty REST API requests, mapped to the pagerduty integration's actions.

An incident update is read from its body: one that acknowledges is
``incident.acknowledge``, one that resolves ``incident.resolve``, one that
reassigns or escalates ``incident.reassign``. An update that does two of
these, or also changes something no action covers (title, priority,
urgency, the incident's service or type), is refused.

Services, escalation policies and schedules are checked by id with their
``.edit`` action. A new escalation policy or schedule is checked as
``team.manage`` on the one team it is created for, a new service as a change
to the escalation policy it is attached to (which decides its teams). A
body that would also move objects between teams (a service's escalation
policy, an escalation policy's services, any ``teams``) is refused, because
toolpass checks one resource per request.

Account configuration (users, webhooks, rulesets, orchestrations, status
pages, incident types and the like) needs ``account.admin``: an account
owner or global admin may do all of it, so the check is never weaker than
PagerDuty's own, only stricter for the roles that may do part of it.

The integration has no read actions: reads are allowed and marked
untrusted. Writes no action expresses (creating an incident, notes, status
updates, merges) and the account's security settings are left out, as is the
Events API (events.pagerduty.com), which acts on incidents with a routing
key alone.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from toolpass.core.routes import Extractor, Request, Route, RouteTable, endpoint_of_url
from toolpass.integrations.pagerduty.actions import id_ok

__all__ = ["ROUTES"]

DEFAULT_HOST = "api.pagerduty.com"

_ACCOUNT = "account"
_INCIDENT = "incident:{id}"
_SERVICE = "service:{id}"
_POLICY = "escalation_policy:{id}"
_SCHEDULE = "schedule:{id}"
_TEAM = "team:{id}"

# Members of an incident update and the action each needs; "type" (and "id"
# in a bulk update) only say which incident.
_REASSIGN_FIELDS = ("assignments", "escalation_level", "escalation_policy")


def _unresolved(reason: str) -> dict[str, str]:
    return {"_unresolved": reason}


class _BodyError(ValueError):
    pass


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in pairs:
        if k in out:
            raise _BodyError(f"the body names {k!r} twice")
        out[k] = v
    return out


def _reject_constant(name: str) -> Any:
    raise _BodyError(f"the body holds {name}, which is not JSON")


def _json(r: Request) -> Any:
    """The body as JSON, read strictly: an object member given twice, a
    non-standard constant, a body that is not UTF-8 or not declared as JSON,
    or a compressed one raises ``_BodyError`` instead of being guessed at."""
    if r.content_type != "application/json":
        raise _BodyError("the body must be JSON, sent as Content-Type: application/json")
    if r.headers.get("content-encoding", "identity").strip().lower() not in ("", "identity"):
        raise _BodyError("a compressed body is not read")
    if not r.body:
        raise _BodyError("the request has no body")
    try:
        return json.loads(r.body.decode("utf-8"), object_pairs_hook=_no_duplicates, parse_constant=_reject_constant)
    except _BodyError:
        raise
    except (ValueError, UnicodeDecodeError) as e:
        raise _BodyError("the body is not valid JSON") from e


def _wrapped(r: Request, key: str) -> dict[str, Any]:
    """The object a PagerDuty body wraps under ``key`` ({"service": {...}})."""
    doc = _json(r)
    obj = doc.get(key) if isinstance(doc, dict) and set(doc) == {key} else None
    if not isinstance(obj, dict):
        raise _BodyError(f"the body must be one object under {key!r}")
    return obj


def _ref(v: Any, what: str) -> str:
    """The id of a reference object ({"id": "PABC123", "type": ...})."""
    if not isinstance(v, dict) or not isinstance(v.get("id"), str) or not id_ok(v["id"]):
        raise _BodyError(f"{what} must be a reference with a PagerDuty id such as PABC123")
    return str(v["id"])


def _one_ref(v: Any, what: str) -> str:
    if not isinstance(v, list) or len(v) != 1:
        raise _BodyError(f"name exactly one {what}; toolpass checks one per request")
    return _ref(v[0], what)


def _path_ids(vars_: Mapping[str, str], *names: str) -> None:
    for name in names:
        if not id_ok(vars_.get(name, "")):
            raise _BodyError(f"{vars_.get(name, '')!r} is not a PagerDuty id such as PABC123")


def _guard(fn: Any) -> Extractor:
    """An extractor whose ``_BodyError`` becomes the refusal's reason."""

    def extract(r: Request, vars_: Mapping[str, str]) -> Mapping[str, str] | None:
        try:
            out: Mapping[str, str] = fn(r, vars_)
        except _BodyError as e:
            return _unresolved(str(e))
        return out

    return extract


def _ids(*names: str) -> Extractor:
    """The path's ids, validated; nothing is read from the body."""

    def extract(r: Request, vars_: Mapping[str, str]) -> dict[str, str]:
        _path_ids(vars_, *names)
        return {}

    return _guard(extract)


# -- incidents -----------------------------------------------------------------


def _incident_action(inc: Any, *, bulk: bool) -> str:
    """The one action an incident update needs."""
    if not isinstance(inc, dict):
        raise _BodyError("an incident update must be an object")
    if inc.get("type") not in ("incident", "incident_reference"):
        raise _BodyError("an incident update must carry type incident_reference")
    known = {"type", "status", "resolution", *_REASSIGN_FIELDS} | ({"id"} if bulk else set())
    other = sorted(set(inc) - known)
    if other:
        raise _BodyError(f"toolpass has no action for changing an incident's {', '.join(other)}; acknowledge, resolve or reassign on its own")
    actions: set[str] = set()
    if "status" in inc:
        status = inc["status"]
        if status == "acknowledged":
            actions.add("incident.acknowledge")
        elif status == "resolved":
            actions.add("incident.resolve")
        else:
            raise _BodyError("an incident's status may be set to acknowledged or resolved")
    if "resolution" in inc and inc.get("status") != "resolved":
        raise _BodyError("a resolution goes with status resolved")
    if any(k in inc for k in _REASSIGN_FIELDS):
        actions.add("incident.reassign")
    if not actions:
        raise _BodyError("the update acknowledges, resolves or reassigns nothing")
    if len(actions) > 1:
        raise _BodyError("acknowledging, resolving and reassigning are separate actions; send one per request")
    return actions.pop()


def _incident_update(r: Request, vars_: Mapping[str, str]) -> dict[str, str]:
    _path_ids(vars_, "id")
    return {"_action": _incident_action(_wrapped(r, "incident"), bulk=False)}


def _incidents_update(r: Request, vars_: Mapping[str, str]) -> dict[str, str]:
    """PUT /incidents: one incident per request, its id from the body."""
    doc = _json(r)
    if not isinstance(doc, dict) or set(doc) != {"incidents"}:
        raise _BodyError("the body must be one list under 'incidents'")
    items = doc["incidents"]
    if not isinstance(items, list) or len(items) != 1:
        raise _BodyError("update one incident per request; toolpass checks one incident at a time")
    inc = items[0]
    action = _incident_action(inc, bulk=True)
    iid = inc.get("id")
    if not isinstance(iid, str) or not id_ok(iid):
        raise _BodyError("the incident's id must be a PagerDuty id such as PABC123")
    return {"_action": action, "id": iid}


def _alert_change(alert: Any) -> None:
    if not isinstance(alert, dict):
        raise _BodyError("an alert update must be an object")
    if "incident" in alert:
        raise _BodyError("moving an alert to another incident changes two incidents; toolpass checks one per request")
    if alert.get("status") != "resolved" or not set(alert) <= {"id", "type", "status"}:
        raise _BodyError("an alert update may only resolve the alert")


def _alert_update(r: Request, vars_: Mapping[str, str]) -> dict[str, str]:
    _path_ids(vars_, "id")
    alert = _wrapped(r, "alert")
    _alert_change(alert)
    if "id" in alert and alert["id"] != vars_["alert_id"]:
        raise _BodyError("the body names another alert than the one in the path")
    return {}


def _alerts_update(r: Request, vars_: Mapping[str, str]) -> dict[str, str]:
    _path_ids(vars_, "id")
    doc = _json(r)
    if not isinstance(doc, dict) or set(doc) != {"alerts"} or not isinstance(doc["alerts"], list) or not doc["alerts"]:
        raise _BodyError("the body must be a non-empty list under 'alerts'")
    for alert in doc["alerts"]:
        _alert_change(alert)
    return {}


# -- services, escalation policies, schedules ----------------------------------


def _update(wrapper: str, *forbidden: str) -> Extractor:
    """PUT on an object by id: the body may not name another id for it, nor
    members that move objects between teams."""

    def extract(r: Request, vars_: Mapping[str, str]) -> dict[str, str]:
        _path_ids(vars_, "id")
        obj = _wrapped(r, wrapper)
        if "id" in obj and obj["id"] != vars_["id"]:
            raise _BodyError(f"the body names {wrapper} {obj['id']!r}, not the one in the path")
        moved = [k for k in forbidden if k in obj]
        if moved:
            raise _BodyError(f"{wrapper}.{moved[0]} moves objects between teams; toolpass checks one resource per request, so change it on its own")
        return {}

    return _guard(extract)


def _create_in_team(wrapper: str, *forbidden: str) -> Extractor:
    """POST of a new escalation policy or schedule: ``team.manage`` on the one
    team it is created for, ``account.admin`` when it belongs to none."""

    def extract(r: Request, vars_: Mapping[str, str]) -> dict[str, str]:
        obj = _wrapped(r, wrapper)
        attached = [k for k in forbidden if k in obj]
        if attached:
            raise _BodyError(f"{wrapper}.{attached[0]} attaches existing objects to the new {wrapper}; attach them in a separate request")
        teams = obj.get("teams")
        if teams is None or teams == []:
            return {"_action": "account.admin", "target": _ACCOUNT}
        return {"target": "team:" + _one_ref(teams, "team")}

    return _guard(extract)


def _service_create(r: Request, vars_: Mapping[str, str]) -> dict[str, str]:
    """A new service is checked as a change to its escalation policy, which
    decides the service's teams."""
    obj = _wrapped(r, "service")
    if "teams" in obj:
        raise _BodyError("a service takes its teams from its escalation policy; leave teams out")
    return {"target": "escalation_policy:" + _ref(obj.get("escalation_policy"), "service.escalation_policy")}


def _maintenance_create(r: Request, vars_: Mapping[str, str]) -> dict[str, str]:
    obj = _wrapped(r, "maintenance_window")
    if "teams" in obj:
        raise _BodyError("a maintenance window is checked on its service; leave teams out")
    return {"target": "service:" + _one_ref(obj.get("services"), "service")}


def _extension_create(r: Request, vars_: Mapping[str, str]) -> dict[str, str]:
    obj = _wrapped(r, "extension")
    objs = obj.get("extension_objects")
    if not isinstance(objs, list) or len(objs) != 1 or not isinstance(objs[0], dict) or objs[0].get("type") not in ("service", "service_reference"):
        raise _BodyError("an extension must name exactly one service in extension_objects")
    return {"target": "service:" + _ref(objs[0], "extension_objects[0]")}


_TAGGED = {
    "users": ("account.admin", _ACCOUNT),
    "teams": ("team.manage", "team:{id}"),
    "escalation_policies": ("escalation_policy.edit", "escalation_policy:{id}"),
}


def _change_tags(r: Request, vars_: Mapping[str, str]) -> dict[str, str]:
    _path_ids(vars_, "id")
    kind = _TAGGED.get(vars_.get("entity_type", ""))
    if kind is None:
        raise _BodyError("tags can be changed on users, teams and escalation policies")
    return {"_action": kind[0], "target": kind[1].replace("{id}", vars_["id"])}


# -- the table -----------------------------------------------------------------


def _routes() -> list[Route]:
    g = _guard
    inc = _ids("id")
    out: list[Route] = [
        # Incidents: the body says which action.
        Route("PUT", "/incidents/{id}", "incident.acknowledge", _INCIDENT, body=g(_incident_update), description="acknowledge, resolve or reassign"),
        Route("PUT", "/incidents", "incident.acknowledge", _INCIDENT, body=g(_incidents_update), description="one incident per request"),
        Route("PUT", "/incidents/{id}/alerts", "incident.resolve", _INCIDENT, body=g(_alerts_update), description="resolve alerts of the incident"),
        Route("PUT", "/incidents/{id}/alerts/{alert_id}", "incident.resolve", _INCIDENT, body=g(_alert_update), description="resolve one alert"),
        Route("POST", "/incidents/{id}/snooze", "incident.acknowledge", _INCIDENT, body=inc, description="snooze an acknowledged incident"),
        Route("POST", "/incidents/{id}/responder_requests", "incident.reassign", _INCIDENT, sends_out=True, body=inc, description="page more responders"),
        Route("PUT", "/incidents/{id}/responder_requests/cancel", "incident.reassign", _INCIDENT, body=inc),
        # Services.
        Route("POST", "/services", "escalation_policy.edit", "{target}", body=g(_service_create)),
        Route("PUT", "/services/{id}", "service.edit", _SERVICE, body=_update("service", "escalation_policy", "teams")),
        Route("DELETE", "/services/{id}", "service.edit", _SERVICE, body=_ids("id")),
        Route("POST", "/maintenance_windows", "service.maintenance", "{target}", body=g(_maintenance_create)),
        Route("POST", "/extensions", "service.edit", "{target}", sends_out=True, body=g(_extension_create), description="a webhook extension on one service"),
        # Escalation policies and schedules.
        Route("POST", "/escalation_policies", "team.manage", "{target}", body=_create_in_team("escalation_policy", "services")),
        Route("PUT", "/escalation_policies/{id}", "escalation_policy.edit", _POLICY, body=_update("escalation_policy", "services", "teams")),
        Route("DELETE", "/escalation_policies/{id}", "escalation_policy.edit", _POLICY, body=_ids("id")),
        Route("POST", "/schedules", "team.manage", "{target}", body=_create_in_team("schedule", "escalation_policies")),
        Route("PUT", "/schedules/{id}", "schedule.edit", _SCHEDULE, body=_update("schedule", "escalation_policies", "teams")),
        Route("DELETE", "/schedules/{id}", "schedule.edit", _SCHEDULE, body=_ids("id")),
        Route("POST", "/schedules/{id}/overrides", "schedule.override", _SCHEDULE, body=_ids("id")),
        Route("DELETE", "/schedules/{id}/overrides/{override_id}", "schedule.override", _SCHEDULE, body=_ids("id")),
        Route("POST", "/v3/schedules", "team.manage", "{target}", body=_create_in_team("schedule")),
        Route("PUT", "/v3/schedules/{id}", "schedule.edit", _SCHEDULE, body=_update("schedule", "teams")),
        Route("POST", "/{entity_type}/{id}/change_tags", "account.admin", "{target}", body=g(_change_tags)),
        # Teams.
        Route("PUT", "/teams/{id}", "team.manage", _TEAM, body=_ids("id")),
        Route("PUT", "/teams/{id}/users/{user_id}", "team.manage", _TEAM, body=_ids("id"), description="add a member or change a member's team role"),
        Route("DELETE", "/teams/{id}/users/{user_id}", "team.manage", _TEAM, body=_ids("id")),
        Route("POST", "/teams/{id}/notification_subscriptions", "team.manage", _TEAM, body=_ids("id")),
        Route("POST", "/teams/{id}/notification_subscriptions/unsubscribe", "team.manage", _TEAM, body=_ids("id")),
        # Reads sent as POST: nothing changes; the results are untrusted.
        Route("POST", "/schedules/preview", None, "", effect="read", description="computes a schedule without saving it"),
        Route("POST", "/templates/{id}/render", None, "", effect="read", description="renders a template without sending it"),
    ]
    for path in (
        "/analytics/metrics/incidents/all",
        "/analytics/metrics/incidents/escalation_policies",
        "/analytics/metrics/incidents/escalation_policies/all",
        "/analytics/metrics/incidents/services",
        "/analytics/metrics/incidents/services/all",
        "/analytics/metrics/incidents/teams",
        "/analytics/metrics/incidents/teams/all",
        "/analytics/metrics/pd_advance_usage/features",
        "/analytics/metrics/responders/all",
        "/analytics/metrics/responders/teams",
        "/analytics/metrics/users/all",
        "/analytics/raw/incidents",
        "/analytics/raw/responders/{responder_id}/incidents",
        "/analytics/raw/users",
    ):
        out.append(Route("POST", path, None, "", effect="read", description="analytics query"))
    # A service's own configuration: service.edit on the service in the path.
    for method, path in (
        ("POST", "/services/{id}/integrations"),
        ("PUT", "/services/{id}/integrations/{integration_id}"),
        ("POST", "/services/{id}/rules"),
        ("POST", "/services/{id}/rules/convert"),
        ("PUT", "/services/{id}/rules/{rule_id}"),
        ("DELETE", "/services/{id}/rules/{rule_id}"),
        ("PUT", "/services/{id}/custom_fields/values"),
        ("PUT", "/services/{id}/enablements/{feature_name}"),
    ):
        out.append(Route(method, path, "service.edit", _SERVICE, body=_ids("id")))
    for method, path in (
        ("PUT", "/event_orchestrations/services/{service_id}"),
        ("PUT", "/event_orchestrations/services/{service_id}/active"),
        ("POST", "/event_orchestrations/services/{service_id}/cache_variables"),
        ("PUT", "/event_orchestrations/services/{service_id}/cache_variables/{cache_variable_id}"),
        ("DELETE", "/event_orchestrations/services/{service_id}/cache_variables/{cache_variable_id}"),
        ("PUT", "/event_orchestrations/services/{service_id}/cache_variables/{cache_variable_id}/data"),
        ("DELETE", "/event_orchestrations/services/{service_id}/cache_variables/{cache_variable_id}/data"),
        ("POST", "/recommendations/event_orchestrations/services/{service_id}/rules/{recommendation_id}/dismiss"),
        ("POST", "/recommendations/event_orchestrations/services/{service_id}/rules/{recommendation_id}/accept"),
        ("DELETE", "/recommendations/event_orchestrations/services/{service_id}/accepted_rules/{rule_id}"),
    ):
        out.append(Route(method, path, "service.edit", "service:{service_id}", body=_ids("service_id")))
    # A schedule's layers in the v3 API.
    for method, path, action in (
        ("DELETE", "/v3/schedules/{id}", "schedule.edit"),
        ("POST", "/v3/schedules/{id}/custom_shifts", "schedule.edit"),
        ("PUT", "/v3/schedules/{id}/custom_shifts/{custom_shift_id}", "schedule.edit"),
        ("DELETE", "/v3/schedules/{id}/custom_shifts/{custom_shift_id}", "schedule.edit"),
        ("POST", "/v3/schedules/{id}/rotations", "schedule.edit"),
        ("DELETE", "/v3/schedules/{id}/rotations/{rotation_id}", "schedule.edit"),
        ("POST", "/v3/schedules/{id}/rotations/{rotation_id}/events", "schedule.edit"),
        ("PUT", "/v3/schedules/{id}/rotations/{rotation_id}/events/{event_id}", "schedule.edit"),
        ("DELETE", "/v3/schedules/{id}/rotations/{rotation_id}/events/{event_id}", "schedule.edit"),
        ("POST", "/v3/schedules/{id}/overrides", "schedule.override"),
        ("PUT", "/v3/schedules/{id}/overrides/{override_id}", "schedule.override"),
        ("DELETE", "/v3/schedules/{id}/overrides/{override_id}", "schedule.override"),
    ):
        out.append(Route(method, path, action, _SCHEDULE, body=_ids("id")))
    # Account configuration: account.admin.
    for method, path, sends_out in _ACCOUNT_WRITES:
        out.append(Route(method, path, "account.admin", _ACCOUNT, sends_out=sends_out))
    return out


# (method, path, sends data out)
_ACCOUNT_WRITES: tuple[tuple[str, str, bool], ...] = (
    ("POST", "/addons", False),
    ("PUT", "/addons/{id}", False),
    ("DELETE", "/addons/{id}", False),
    ("POST", "/alert_grouping_settings", False),
    ("PUT", "/alert_grouping_settings/{id}", False),
    ("DELETE", "/alert_grouping_settings/{id}", False),
    ("POST", "/automation_actions/actions", False),
    ("PUT", "/automation_actions/actions/{id}", False),
    ("DELETE", "/automation_actions/actions/{id}", False),
    ("POST", "/automation_actions/actions/{id}/invocations", True),
    ("POST", "/automation_actions/actions/{id}/services", False),
    ("DELETE", "/automation_actions/actions/{id}/services/{service_id}", False),
    ("POST", "/automation_actions/actions/{id}/teams", False),
    ("DELETE", "/automation_actions/actions/{id}/teams/{team_id}", False),
    ("POST", "/automation_actions/runners", False),
    ("PUT", "/automation_actions/runners/{id}", False),
    ("DELETE", "/automation_actions/runners/{id}", False),
    ("POST", "/automation_actions/runners/{id}/teams", False),
    ("DELETE", "/automation_actions/runners/{id}/teams/{team_id}", False),
    ("POST", "/business_services", False),
    ("PUT", "/business_services/{id}", False),
    ("DELETE", "/business_services/{id}", False),
    ("POST", "/business_services/{id}/account_subscription", False),
    ("DELETE", "/business_services/{id}/account_subscription", False),
    ("POST", "/business_services/{id}/subscribers", False),
    ("POST", "/business_services/{id}/unsubscribe", False),
    ("PUT", "/business_services/priority_thresholds", False),
    ("DELETE", "/business_services/priority_thresholds", False),
    ("POST", "/change_events", False),
    ("PUT", "/change_events/{id}", False),
    ("POST", "/enrichment/integrations/servicenow", False),
    ("DELETE", "/enrichment/integrations/servicenow/{integration_id}", False),
    ("POST", "/enrichment/integrations/servicenow/{integration_id}/tables", False),
    ("PUT", "/enrichment/integrations/servicenow/{integration_id}/tables/{table_id}", False),
    ("DELETE", "/enrichment/integrations/servicenow/{integration_id}/tables/{table_id}", False),
    ("POST", "/enrichment/integrations/servicenow/{integration_id}/tables/{table_id}/enable", False),
    ("POST", "/enrichment/integrations/servicenow/credentials", False),
    ("PUT", "/enrichment/integrations/servicenow/credentials/{credentials_id}", False),
    ("DELETE", "/enrichment/integrations/servicenow/credentials/{credentials_id}", False),
    ("POST", "/enrichment/query", False),
    ("POST", "/enrichment/schemas", False),
    ("PUT", "/enrichment/schemas/{schema_id}", False),
    ("DELETE", "/enrichment/schemas/{schema_id}", False),
    ("POST", "/enrichment/schemas/{schema_id}/records", False),
    ("DELETE", "/enrichment/schemas/{schema_id}/records/{record_id}", False),
    ("POST", "/enrichment/event_enrichments", False),
    ("PUT", "/enrichment/event_enrichments/default", False),
    ("PUT", "/enrichment/event_enrichments/{id}", False),
    ("DELETE", "/enrichment/event_enrichments/{id}", False),
    ("POST", "/enrichment/event_enrichments/{id}/associations", False),
    ("DELETE", "/enrichment/event_enrichments/{id}/associations", False),
    ("PUT", "/enrichment/event_enrichments/{id}/rules", False),
    ("POST", "/event_orchestrations", False),
    ("PUT", "/event_orchestrations/{id}", False),
    ("DELETE", "/event_orchestrations/{id}", False),
    ("POST", "/event_orchestrations/{id}/integrations", False),
    ("PUT", "/event_orchestrations/{id}/integrations/{integration_id}", False),
    ("DELETE", "/event_orchestrations/{id}/integrations/{integration_id}", False),
    ("POST", "/event_orchestrations/{id}/integrations/migration", False),
    ("PUT", "/event_orchestrations/{id}/global", False),
    ("PUT", "/event_orchestrations/{id}/router", False),
    ("PUT", "/event_orchestrations/{id}/unrouted", False),
    ("POST", "/event_orchestrations/{id}/cache_variables", False),
    ("PUT", "/event_orchestrations/{id}/cache_variables/{cache_variable_id}", False),
    ("DELETE", "/event_orchestrations/{id}/cache_variables/{cache_variable_id}", False),
    ("PUT", "/event_orchestrations/{id}/cache_variables/{cache_variable_id}/data", False),
    ("DELETE", "/event_orchestrations/{id}/cache_variables/{cache_variable_id}/data", False),
    ("PUT", "/event_orchestrations/{id}/enablements/{feature_name}", False),
    ("PUT", "/extensions/{id}", True),
    ("DELETE", "/extensions/{id}", False),
    ("POST", "/extensions/{id}/enable", True),
    ("POST", "/incident_workflows", False),
    ("PUT", "/incident_workflows/{id}", False),
    ("DELETE", "/incident_workflows/{id}", False),
    ("POST", "/incident_workflows/{id}/instances", True),
    ("POST", "/incident_workflows/triggers", False),
    ("PUT", "/incident_workflows/triggers/{id}", False),
    ("DELETE", "/incident_workflows/triggers/{id}", False),
    ("POST", "/incident_workflows/triggers/{id}/services", False),
    ("DELETE", "/incident_workflows/triggers/{trigger_id}/services/{service_id}", False),
    ("POST", "/incidents/types", False),
    ("PUT", "/incidents/types/{type_id_or_name}", False),
    ("POST", "/incidents/types/{type_id_or_name}/custom_fields", False),
    ("PUT", "/incidents/types/{type_id_or_name}/custom_fields/{field_id}", False),
    ("DELETE", "/incidents/types/{type_id_or_name}/custom_fields/{field_id}", False),
    ("POST", "/incidents/types/{type_id_or_name}/custom_fields/{field_id}/field_options", False),
    ("PUT", "/incidents/types/{type_id_or_name}/custom_fields/{field_id}/field_options/{field_option_id}", False),
    ("DELETE", "/incidents/types/{type_id_or_name}/custom_fields/{field_id}/field_options/{field_option_id}", False),
    ("POST", "/incidents/custom_fields", False),
    ("PUT", "/incidents/custom_fields/{field_id}", False),
    ("DELETE", "/incidents/custom_fields/{field_id}", False),
    ("POST", "/incidents/custom_fields/{field_id}/field_options", False),
    ("PUT", "/incidents/custom_fields/{field_id}/field_options/{field_option_id}", False),
    ("DELETE", "/incidents/custom_fields/{field_id}/field_options/{field_option_id}", False),
    ("PUT", "/log_entries/{id}/channel", False),
    ("PUT", "/maintenance_windows/{id}", False),
    ("DELETE", "/maintenance_windows/{id}", False),
    ("POST", "/rulesets", False),
    ("PUT", "/rulesets/{id}", False),
    ("DELETE", "/rulesets/{id}", False),
    ("POST", "/rulesets/{id}/rules", False),
    ("PUT", "/rulesets/{id}/rules/{rule_id}", False),
    ("DELETE", "/rulesets/{id}/rules/{rule_id}", False),
    ("POST", "/service_dependencies/associate", False),
    ("POST", "/service_dependencies/disassociate", False),
    ("POST", "/services/custom_fields", False),
    ("PUT", "/services/custom_fields/{field_id}", False),
    ("DELETE", "/services/custom_fields/{field_id}", False),
    ("POST", "/services/custom_fields/{field_id}/field_options", False),
    ("PUT", "/services/custom_fields/{field_id}/field_options/{field_option_id}", False),
    ("DELETE", "/services/custom_fields/{field_id}/field_options/{field_option_id}", False),
    ("PUT", "/standards/{id}", False),
    ("POST", "/status_pages/{id}/posts", True),
    ("PUT", "/status_pages/{id}/posts/{post_id}", True),
    ("DELETE", "/status_pages/{id}/posts/{post_id}", False),
    ("POST", "/status_pages/{id}/posts/{post_id}/post_updates", True),
    ("PUT", "/status_pages/{id}/posts/{post_id}/post_updates/{post_update_id}", True),
    ("DELETE", "/status_pages/{id}/posts/{post_id}/post_updates/{post_update_id}", False),
    ("PUT", "/status_pages/{id}/posts/{post_id}/postmortem", True),
    ("DELETE", "/status_pages/{id}/posts/{post_id}/postmortem", False),
    ("POST", "/status_pages/{id}/subscriptions", True),
    ("DELETE", "/status_pages/{id}/subscriptions/{subscription_id}", False),
    ("PUT", "/sre_agent/memories/{id}", False),
    ("DELETE", "/sre_agent/memories/{id}", False),
    ("POST", "/tags", False),
    ("DELETE", "/tags/{id}", False),
    ("POST", "/teams", False),
    ("DELETE", "/teams/{id}", False),
    ("PUT", "/teams/{id}/escalation_policies/{escalation_policy_id}", False),
    ("DELETE", "/teams/{id}/escalation_policies/{escalation_policy_id}", False),
    ("POST", "/templates", False),
    ("PUT", "/templates/{id}", False),
    ("DELETE", "/templates/{id}", False),
    ("POST", "/users", False),
    ("PUT", "/users/{id}", False),
    ("DELETE", "/users/{id}", False),
    ("POST", "/users/{id}/contact_methods", False),
    ("PUT", "/users/{id}/contact_methods/{contact_method_id}", False),
    ("DELETE", "/users/{id}/contact_methods/{contact_method_id}", False),
    ("POST", "/users/{id}/notification_rules", False),
    ("PUT", "/users/{id}/notification_rules/{notification_rule_id}", False),
    ("DELETE", "/users/{id}/notification_rules/{notification_rule_id}", False),
    ("POST", "/users/{id}/notification_subscriptions", False),
    ("POST", "/users/{id}/notification_subscriptions/unsubscribe", False),
    ("POST", "/users/{id}/oncall_handoff_notification_rules", False),
    ("PUT", "/users/{id}/oncall_handoff_notification_rules/{oncall_handoff_notification_rule_id}", False),
    ("DELETE", "/users/{id}/oncall_handoff_notification_rules/{oncall_handoff_notification_rule_id}", False),
    ("DELETE", "/users/{id}/sessions", False),
    ("DELETE", "/users/{id}/sessions/{type}/{session_id}", False),
    ("POST", "/users/{id}/status_update_notification_rules", False),
    ("PUT", "/users/{id}/status_update_notification_rules/{status_update_notification_rule_id}", False),
    ("DELETE", "/users/{id}/status_update_notification_rules/{status_update_notification_rule_id}", False),
    ("POST", "/users/{id}/regenerate_private_url_key", False),
    ("POST", "/webhook_subscriptions", True),
    ("PUT", "/webhook_subscriptions/{id}", True),
    ("DELETE", "/webhook_subscriptions/{id}", False),
    ("POST", "/webhook_subscriptions/{id}/enable", True),
    ("POST", "/webhook_subscriptions/{id}/ping", True),
    ("POST", "/webhook_subscriptions/oauth_clients", False),
    ("PUT", "/webhook_subscriptions/oauth_clients/{id}", False),
    ("DELETE", "/webhook_subscriptions/oauth_clients/{id}", False),
    ("POST", "/workflows/integrations/{integration_id}/connections", False),
    ("PATCH", "/workflows/integrations/{integration_id}/connections/{id}", False),
    ("DELETE", "/workflows/integrations/{integration_id}/connections/{id}", False),
)


# The Events API hosts. A routing key alone triggers, acknowledges and
# resolves incidents there, so its writes are located and refused.
_EVENTS_HOSTS = ("events.pagerduty.com", "events.eu.pagerduty.com")
_API_HOSTS = (DEFAULT_HOST, "api.eu.pagerduty.com")


def _locate(settings: Any) -> Sequence[tuple[str, str]]:
    """The connection's ``url``, by default https://api.pagerduty.com, and,
    for PagerDuty's own API hosts, the Events API hosts."""
    get = getattr(settings, "get", None)
    url = (get("url") if callable(get) else "") or "https://" + DEFAULT_HOST
    ep = endpoint_of_url(url)
    if ep is None:
        return []
    out = [ep]
    if ep[0].lower() in _API_HOSTS:
        out += [(h, "") for h in _EVENTS_HOSTS]
    return out


_NO_ACTION = "the pagerduty integration has no action for this; it is refused until one exists"
_EVENTS = (
    "the Events API (served on events.pagerduty.com) triggers, acknowledges and resolves incidents with a routing key alone; no user permission governs it"
)

ROUTES = RouteTable(
    "pagerduty",
    _routes(),
    claims=("/",),
    unrouted={
        ("POST", "/incidents"): "creating an incident pages the service's on-call; " + _NO_ACTION,
        ("PUT", "/incidents/{id}/merge"): "a merge resolves every source incident into the target; toolpass checks one incident per request",
        ("POST", "/incidents/{id}/notes"): "incident notes: " + _NO_ACTION,
        ("PUT", "/incidents/{id}/notes/{note_id}"): "incident notes: " + _NO_ACTION,
        ("DELETE", "/incidents/{id}/notes/{note_id}"): "incident notes: " + _NO_ACTION,
        ("POST", "/incidents/{id}/status_updates"): "status updates notify stakeholders: " + _NO_ACTION,
        ("POST", "/incidents/{id}/status_updates/subscribers"): "status update subscriptions: " + _NO_ACTION,
        ("POST", "/incidents/{id}/status_updates/unsubscribe"): "status update subscriptions: " + _NO_ACTION,
        ("PUT", "/incidents/{id}/custom_fields/values"): "an incident's custom field values: " + _NO_ACTION,
        ("PUT", "/incidents/{id}/business_services/{business_service_id}/impacts"): "business service impact: " + _NO_ACTION,
        ("POST", "/ip_allow_lists"): "the account's security settings are left to the account owner",
        ("PUT", "/ip_allow_lists/{id}"): "the account's security settings are left to the account owner",
        ("DELETE", "/ip_allow_lists/{id}"): "the account's security settings are left to the account owner",
        ("PUT", "/session_configurations"): "the account's security settings are left to the account owner",
        ("DELETE", "/session_configurations"): "the account's security settings are left to the account owner",
        ("DELETE", "/oauth_delegations"): "revoking users' OAuth sessions is left to the account owner",
        # The Events API: v2 events and change events, the v1 generic and integration endpoints.
        ("*", "/v2/enqueue"): _EVENTS,
        ("*", "/v2/change/enqueue"): _EVENTS,
        ("*", "/integration"): _EVENTS,
        ("*", "/integration/**"): _EVENTS,
        ("*", "/generic"): _EVENTS,
        ("*", "/generic/**"): _EVENTS,
    },
    locate=_locate,
)

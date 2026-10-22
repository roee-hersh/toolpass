"""Datadog API requests, mapped to the datadog integration's actions.

Monitors, dashboards, SLOs and notebooks are checked by id (``monitor.edit``
on ``monitor:123``, ``raw:slos_read`` on ``slo:<id>``), so both the user's
roles and the asset's restriction policy apply. Creating one, and listing
them, check the permission on the org (``raw:monitors_write`` on ``org``). A
downtime that names a monitor is ``monitor.mute`` on it; one by scope or by
monitor tags is ``raw:monitors_downtime`` on the org. Users, roles, keys,
logs and metrics configuration map to the one permission Datadog's API
description names for the operation, as ``raw:<permission>`` on the org;
reading log data is ``logs.read``. Reading API and application keys, which
returns the keys, is ``raw:api_keys_read`` and ``raw:org_app_keys_read``.

Refused: writes to the other assets that carry restriction policies the
integration does not read (synthetic tests, security rules, workflows,
on-call schedules, logs pipelines, ...), writes the description gives no
permission for or two permissions at once, sharing dashboards outside the
org, and data intake, which authenticates with the API key alone. A body
that names a second asset (an id beside the path's, several dashboards in a
bulk delete) is refused too.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from toolpass.core.routes import Extractor, Request, Route, RouteTable, endpoint_of_url
from toolpass.integrations.datadog.actions import id_ok

__all__ = ["DEFAULT_HOST", "ROUTES"]

DEFAULT_HOST = "api.datadoghq.com"

_ORG = "org"


def _raw(permission: str) -> str:
    return "raw:" + permission


# The assets the integration checks with their restriction policies: the
# path variable that names one, and the actions that read and change it.
_ASSETS: dict[str, tuple[str, str]] = {
    "monitor": ("monitor.read", "monitor.edit"),
    "dashboard": ("dashboard.read", "dashboard.edit"),
    "slo": (_raw("slos_read"), "slo.edit"),
    "notebook": (_raw("notebooks_read"), "notebook.edit"),
}


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


def _guard(fn: Any) -> Extractor:
    """An extractor whose ``_BodyError`` becomes the refusal's reason."""

    def extract(r: Request, vars_: Mapping[str, str]) -> Mapping[str, str] | None:
        try:
            out: Mapping[str, str] = fn(r, vars_)
        except _BodyError as e:
            return {"_unresolved": str(e)}
        return out

    return extract


def _asset_id(v: Any, what: str) -> str:
    """An asset id from a body: a string or an integer (monitor ids are
    numbers), in the form the integration checks."""
    if isinstance(v, bool) or not isinstance(v, (str, int)) or not id_ok(str(v)):
        raise _BodyError(f"{what} must be a Datadog asset id")
    return str(v)


def _path_id(name: str) -> Extractor:
    """The asset id in the path, validated; nothing is read from the body."""

    def extract(r: Request, vars_: Mapping[str, str]) -> dict[str, str]:
        if not id_ok(vars_.get(name, "")):
            raise _BodyError(f"{vars_.get(name, '')!r} is not a Datadog asset id")
        return {}

    return _guard(extract)


def _same_id(name: str) -> Extractor:
    """An update by id: the body may not name another asset (its ``id`` or
    ``data.id``) than the path."""

    def extract(r: Request, vars_: Mapping[str, str]) -> dict[str, str]:
        want = vars_.get(name, "")
        if not id_ok(want):
            raise _BodyError(f"{want!r} is not a Datadog asset id")
        doc = _json(r)
        if not isinstance(doc, dict):
            raise _BodyError("the body must be a JSON object")
        data = doc.get("data")
        for where, v in (("id", doc.get("id")), ("data.id", data.get("id") if isinstance(data, dict) else None)):
            if v is not None and _asset_id(v, where) != want:
                raise _BodyError(f"the body's {where} names another asset than the path")
        return {}

    return _guard(extract)


def _downtime_target(monitor_id: Any) -> dict[str, str]:
    """A downtime on one monitor is ``monitor.mute`` on it; one by scope or
    monitor tags needs ``monitors_downtime`` on the org."""
    if monitor_id is not None:
        return {"_action": "monitor.mute", "target": "monitor:" + _asset_id(monitor_id, "monitor_id")}
    return {"target": _ORG}


def _downtime_v1(r: Request, vars_: Mapping[str, str]) -> dict[str, str]:
    doc = _json(r)
    if not isinstance(doc, dict):
        raise _BodyError("the body must be a JSON object")
    if doc.get("id") is not None and "downtime_id" in vars_ and str(doc["id"]) != vars_["downtime_id"]:
        raise _BodyError("the body's id names another downtime than the path")
    return _downtime_target(doc.get("monitor_id"))


def _downtime_v2(r: Request, vars_: Mapping[str, str]) -> dict[str, str]:
    doc = _json(r)
    data = doc.get("data") if isinstance(doc, dict) else None
    attrs = data.get("attributes") if isinstance(data, dict) else None
    if not isinstance(data, dict) or not isinstance(attrs, dict):
        raise _BodyError("the body must carry data.attributes")
    if data.get("id") is not None and "downtime_id" in vars_ and data["id"] != vars_["downtime_id"]:
        raise _BodyError("the body's data.id names another downtime than the path")
    ident = attrs.get("monitor_identifier")
    if ident is None:
        return {"target": _ORG}
    if not isinstance(ident, dict) or set(ident) not in ({"monitor_id"}, {"monitor_tags"}):
        raise _BodyError("monitor_identifier must hold either monitor_id or monitor_tags")
    return _downtime_target(ident.get("monitor_id"))


def _without_roles(r: Request, vars_: Mapping[str, str]) -> dict[str, str]:
    """Creating a user or a service account: giving it roles also needs
    ``user_access_manage``, and toolpass checks one permission per request,
    so the roles are assigned afterwards (POST /api/v2/roles/{id}/users)."""
    doc = _json(r)
    if not isinstance(doc, dict):
        raise _BodyError("the body must be a JSON object")
    data = doc.get("data")
    rels = data.get("relationships") if isinstance(data, dict) else None
    roles = rels.get("roles") if isinstance(rels, dict) else None
    if doc.get("access_role") is not None or (roles is not None and roles != {"data": []}):
        raise _BodyError("create the account without roles, then assign them with POST /api/v2/roles/{role_id}/users")
    return {}


def _one_dashboard(r: Request, vars_: Mapping[str, str]) -> dict[str, str]:
    """A bulk delete or restore of dashboards: one per request."""
    doc = _json(r)
    items = doc.get("data") if isinstance(doc, dict) else None
    if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict) or items[0].get("type") != "dashboard":
        raise _BodyError("name one dashboard per request; toolpass checks one dashboard at a time")
    return {"dashboard_id": _asset_id(items[0].get("id"), "data[0].id")}


def _one_slo(r: Request, vars_: Mapping[str, str]) -> dict[str, str]:
    """A bulk delete of SLO timeframes: one SLO per request."""
    doc = _json(r)
    if not isinstance(doc, dict) or len(doc) != 1:
        raise _BodyError("name one SLO per request; toolpass checks one SLO at a time")
    return {"slo_id": _asset_id(next(iter(doc)), "the SLO id")}


def _restriction_policy(*, write: bool) -> Extractor:
    """A restriction policy, by its resource id (``dashboard:abc-def-ghi``):
    changing it needs the asset's edit action, reading it the read action.
    Policies of assets the integration does not check are refused."""

    def extract(r: Request, vars_: Mapping[str, str]) -> dict[str, str]:
        typ, sep, aid = vars_.get("resource_id", "").partition(":")
        if not sep or typ not in _ASSETS or not id_ok(aid):
            raise _BodyError("toolpass checks restriction policies of monitors, dashboards, SLOs and notebooks, named type:id")
        if write and r.method.upper() == "POST":
            doc = _json(r)
            data = doc.get("data") if isinstance(doc, dict) else None
            if not isinstance(data, dict) or data.get("id") != vars_["resource_id"]:
                raise _BodyError("the body's data.id must be the policy's resource id in the path")
        read, edit = _ASSETS[typ]
        return {"_action": edit if write else read, "target": f"{typ}:{aid}"}

    return _guard(extract)


_POLICY_READ = "the read action of the asset the resource id names (monitor, dashboard, slo, notebook)"
_POLICY_EDIT = "the edit action of the asset the resource id names (monitor, dashboard, slo, notebook)"


def _asset_routes(typ: str, item: str, var: str) -> list[Route]:
    """GET, PUT and DELETE of one asset by id."""
    read, edit = _ASSETS[typ]
    res = f"{typ}:{{{var}}}"
    return [
        Route("GET", item, read, res, body=_path_id(var)),
        Route("PUT", item, edit, res, body=_same_id(var)),
        Route("DELETE", item, edit, res, body=_path_id(var)),
    ]


def _routes() -> list[Route]:
    g = _guard
    out: list[Route] = []
    # Monitors.
    out += _asset_routes("monitor", "/api/v1/monitor/{monitor_id}", "monitor_id")
    out += [
        Route("GET", "/api/v1/monitor", _raw("monitors_read"), _ORG),
        Route("POST", "/api/v1/monitor", _raw("monitors_write"), _ORG),
        Route("GET", "/api/v1/monitor/can_delete", _raw("monitors_read"), _ORG),
        Route("GET", "/api/v1/monitor/groups/search", _raw("monitors_read"), _ORG),
        Route("GET", "/api/v1/monitor/search", _raw("monitors_read"), _ORG),
        Route("POST", "/api/v1/monitor/validate", _raw("monitors_read"), _ORG, effect="read", description="validates a definition; nothing is saved"),
        Route("GET", "/api/v1/monitor/{monitor_id}/downtimes", "monitor.read", "monitor:{monitor_id}", body=_path_id("monitor_id")),
        Route("POST", "/api/v1/monitor/{monitor_id}/validate", "monitor.read", "monitor:{monitor_id}", effect="read", body=_path_id("monitor_id")),
        # A read: the permission is checked on the org, as the other downtime reads,
        # since monitors_downtime on a monitor would be checked as a change to it.
        Route("GET", "/api/v2/monitor/{monitor_id}/downtime_matches", _raw("monitors_downtime"), _ORG, body=_path_id("monitor_id")),
    ]
    # Downtimes: on one monitor, monitor.mute on it; by scope, the permission on the org.
    out += [
        Route("GET", "/api/v1/downtime", _raw("monitors_read"), _ORG),
        Route("POST", "/api/v1/downtime", _raw("monitors_downtime"), "{target}", body=g(_downtime_v1)),
        Route("POST", "/api/v1/downtime/cancel/by_scope", _raw("monitors_downtime"), _ORG, effect="destructive"),
        Route("GET", "/api/v1/downtime/{downtime_id}", _raw("monitors_read"), _ORG),
        Route("PUT", "/api/v1/downtime/{downtime_id}", _raw("monitors_downtime"), "{target}", body=g(_downtime_v1)),
        Route("DELETE", "/api/v1/downtime/{downtime_id}", _raw("monitors_downtime"), _ORG),
        Route("GET", "/api/v2/downtime", _raw("monitors_downtime"), _ORG),
        Route("POST", "/api/v2/downtime", _raw("monitors_downtime"), "{target}", body=g(_downtime_v2)),
        Route("GET", "/api/v2/downtime/{downtime_id}", _raw("monitors_downtime"), _ORG),
        Route("PATCH", "/api/v2/downtime/{downtime_id}", _raw("monitors_downtime"), "{target}", body=g(_downtime_v2)),
        Route("DELETE", "/api/v2/downtime/{downtime_id}", _raw("monitors_downtime"), _ORG),
    ]
    # Monitor configuration policies and templates belong to no monitor.
    for method, path in (
        ("POST", "/api/v2/monitor/notification_rule"),
        ("POST", "/api/v2/monitor/policy"),
        ("PATCH", "/api/v2/monitor/policy/{policy_id}"),
        ("DELETE", "/api/v2/monitor/policy/{policy_id}"),
        ("POST", "/api/v2/monitor/template"),
        ("PUT", "/api/v2/monitor/template/{template_id}"),
        ("DELETE", "/api/v2/monitor/template/{template_id}"),
    ):
        out.append(Route(method, path, _raw("monitor_config_policy_write"), _ORG))
    for path in ("/api/v2/monitor/template/validate", "/api/v2/monitor/template/{template_id}/validate"):
        out.append(Route("POST", path, _raw("monitor_config_policy_write"), _ORG, effect="read", description="validates a template; nothing is saved"))
    # Dashboards and dashboard lists.
    out += _asset_routes("dashboard", "/api/v1/dashboard/{dashboard_id}", "dashboard_id")
    out += [
        Route("GET", "/api/v1/dashboard", _raw("dashboards_read"), _ORG),
        Route("POST", "/api/v1/dashboard", _raw("dashboards_write"), _ORG),
        Route("DELETE", "/api/v1/dashboard", "dashboard.edit", "dashboard:{dashboard_id}", body=g(_one_dashboard)),
        Route("PATCH", "/api/v1/dashboard", "dashboard.edit", "dashboard:{dashboard_id}", body=g(_one_dashboard), description="restore a deleted dashboard"),
        Route("GET", "/api/v2/dashboard/{dashboard_id}/shared", "dashboard.read", "dashboard:{dashboard_id}", body=_path_id("dashboard_id")),
        Route("GET", "/api/v1/dashboard/lists/manual", _raw("dashboards_read"), _ORG),
        Route("POST", "/api/v1/dashboard/lists/manual", _raw("dashboards_write"), _ORG),
        Route("GET", "/api/v1/dashboard/lists/manual/{list_id}", _raw("dashboards_read"), _ORG),
        Route("PUT", "/api/v1/dashboard/lists/manual/{list_id}", _raw("dashboards_write"), _ORG),
        Route("DELETE", "/api/v1/dashboard/lists/manual/{list_id}", _raw("dashboards_write"), _ORG),
    ]
    for method in ("GET", "POST", "PUT", "DELETE"):
        perm = "dashboards_read" if method == "GET" else "dashboards_write"
        out.append(
            Route(
                method, "/api/v2/dashboard/lists/manual/{dashboard_list_id}/dashboards", _raw(perm), _ORG, description="a list's items, as the v1 list itself"
            )
        )
    # SLOs.
    out += _asset_routes("slo", "/api/v1/slo/{slo_id}", "slo_id")
    out += [
        Route("GET", "/api/v1/slo", _raw("slos_read"), _ORG),
        Route("POST", "/api/v1/slo", _raw("slos_write"), _ORG),
        Route("POST", "/api/v1/slo/bulk_delete", "slo.edit", "slo:{slo_id}", effect="destructive", body=g(_one_slo)),
        Route("GET", "/api/v1/slo/can_delete", _raw("slos_read"), _ORG),
        Route("GET", "/api/v1/slo/search", _raw("slos_read"), _ORG),
        Route("GET", "/api/v1/slo/correction", _raw("slos_read"), _ORG),
        Route("GET", "/api/v1/slo/correction/{slo_correction_id}", _raw("slos_read"), _ORG),
        Route("GET", "/api/v1/slo/{slo_id}/corrections", _raw("slos_read"), "slo:{slo_id}", body=_path_id("slo_id")),
        Route("GET", "/api/v1/slo/{slo_id}/history", _raw("slos_read"), "slo:{slo_id}", body=_path_id("slo_id")),
        Route("GET", "/api/v2/slo/{slo_id}/status", _raw("slos_read"), "slo:{slo_id}", body=_path_id("slo_id")),
        Route("POST", "/api/v2/slo/report", _raw("slos_read"), _ORG, effect="read", description="starts a report of SLO history to download"),
    ]
    # Notebooks.
    out += _asset_routes("notebook", "/api/v1/notebooks/{notebook_id}", "notebook_id")
    out += [
        Route("GET", "/api/v1/notebooks", _raw("notebooks_read"), _ORG),
        Route("POST", "/api/v1/notebooks", _raw("notebooks_write"), _ORG),
    ]
    # Restriction policies of the assets above.
    out += [
        Route("GET", "/api/v2/restriction_policy/{resource_id}", "dashboard.read", "{target}", body=_restriction_policy(write=False), description=_POLICY_READ),
        Route("POST", "/api/v2/restriction_policy/{resource_id}", "dashboard.edit", "{target}", body=_restriction_policy(write=True), description=_POLICY_EDIT),
        Route(
            "DELETE", "/api/v2/restriction_policy/{resource_id}", "dashboard.edit", "{target}", body=_restriction_policy(write=True), description=_POLICY_EDIT
        ),
    ]
    # Reading data.
    out += [
        Route("POST", "/api/v1/logs-queries/list", "logs.read", _ORG, effect="read"),
        Route("GET", "/api/v2/logs/archive_searches/{archive_search_id}", "logs.read", _ORG, description="an archive search's logs"),
        Route("GET", "/api/v2/logs/events", "logs.read", _ORG),
        Route("POST", "/api/v2/logs/events/search", "logs.read", _ORG, effect="read"),
        Route("POST", "/api/v2/logs/analytics/aggregate", "logs.read", _ORG, effect="read"),
        Route("GET", "/api/v1/query", _raw("timeseries_query"), _ORG),
        Route("POST", "/api/v2/query/timeseries", _raw("timeseries_query"), _ORG, effect="read"),
        Route("POST", "/api/v2/query/scalar", _raw("timeseries_query"), _ORG, effect="read"),
        Route("GET", "/api/v1/events", _raw("events_read"), _ORG),
        Route("GET", "/api/v1/events/{event_id}", _raw("events_read"), _ORG),
        Route("GET", "/api/v2/events", _raw("events_read"), _ORG),
        Route("POST", "/api/v2/events/search", _raw("events_read"), _ORG, effect="read"),
        Route("GET", "/api/v2/events/{event_id}", _raw("events_read"), _ORG),
        Route("POST", "/api/v2/spans/analytics/aggregate", _raw("apm_read"), _ORG, effect="read"),
        Route("GET", "/api/v2/spans/events", _raw("apm_read"), _ORG),
        Route("POST", "/api/v2/spans/events/search", _raw("apm_read"), _ORG, effect="read"),
        Route("GET", "/api/v2/trace/{trace_id}", _raw("apm_read"), _ORG),
        Route("GET", "/api/v2/pruned_trace/{trace_id}", _raw("apm_read"), _ORG),
    ]
    # Users, roles and keys.
    um = "users.manage"
    out += [
        Route("POST", "/api/v1/user", _raw("user_access_invite"), _ORG, body=g(_without_roles)),
        Route("POST", "/api/v2/users", _raw("user_access_invite"), _ORG, body=g(_without_roles)),
        Route("PATCH", "/api/v2/users/{user_id}", um, _ORG),
        Route("DELETE", "/api/v2/users/{user_id}", um, _ORG, description="disables the user"),
        Route("DELETE", "/api/v2/users/{user_id}/invitations", _raw("user_access_invite"), _ORG),
        Route("PATCH", "/api/v2/users/{user_id}/relationships/identity_providers", um, _ORG),
        Route("POST", "/api/v2/user_invitations", _raw("user_access_invite"), _ORG, sends_out=True, description="emails invitations"),
        Route("POST", "/api/v2/roles", um, _ORG),
        Route("PATCH", "/api/v2/roles/{role_id}", um, _ORG),
        Route("DELETE", "/api/v2/roles/{role_id}", um, _ORG, description="the description names no permission; as every other role change"),
        Route("POST", "/api/v2/roles/{role_id}/clone", um, _ORG),
        Route("POST", "/api/v2/roles/{role_id}/permissions", um, _ORG),
        Route("DELETE", "/api/v2/roles/{role_id}/permissions", um, _ORG),
        Route("POST", "/api/v2/roles/{role_id}/users", um, _ORG),
        Route("DELETE", "/api/v2/roles/{role_id}/users", um, _ORG),
        Route("POST", "/api/v2/authn_mappings", um, _ORG),
        Route("PATCH", "/api/v2/authn_mappings/{authn_mapping_id}", um, _ORG),
        Route("DELETE", "/api/v2/authn_mappings/{authn_mapping_id}", um, _ORG),
        # Reading keys returns the secrets themselves (v1 lists every key in full).
        Route("GET", "/api/v1/api_key", _raw("api_keys_read"), _ORG),
        Route("GET", "/api/v1/api_key/{key}", _raw("api_keys_read"), _ORG),
        Route("GET", "/api/v2/api_keys", _raw("api_keys_read"), _ORG),
        Route("GET", "/api/v2/api_keys/{api_key_id}", _raw("api_keys_read"), _ORG),
        Route("GET", "/api/v1/application_key", _raw("org_app_keys_read"), _ORG),
        Route("GET", "/api/v1/application_key/{key}", _raw("org_app_keys_read"), _ORG),
        Route("GET", "/api/v2/application_keys", _raw("org_app_keys_read"), _ORG),
        Route("GET", "/api/v2/application_keys/{app_key_id}", _raw("org_app_keys_read"), _ORG),
        Route("POST", "/api/v1/api_key", "apikeys.manage", _ORG),
        Route("PUT", "/api/v1/api_key/{key}", "apikeys.manage", _ORG),
        Route("DELETE", "/api/v1/api_key/{key}", _raw("api_keys_delete"), _ORG),
        Route("POST", "/api/v2/api_keys", "apikeys.manage", _ORG),
        Route("PATCH", "/api/v2/api_keys/{api_key_id}", "apikeys.manage", _ORG),
        Route("DELETE", "/api/v2/api_keys/{api_key_id}", _raw("api_keys_delete"), _ORG),
        # Any user's application key: the org-wide permission, not user_app_keys,
        # which covers only the caller's own keys (the agent's, not the user's).
        Route("PUT", "/api/v1/application_key/{key}", _raw("org_app_keys_write"), _ORG),
        Route("DELETE", "/api/v1/application_key/{key}", _raw("org_app_keys_write"), _ORG),
        Route("PATCH", "/api/v2/application_keys/{app_key_id}", _raw("org_app_keys_write"), _ORG),
        Route("DELETE", "/api/v2/application_keys/{app_key_id}", _raw("org_app_keys_write"), _ORG),
    ]
    out.append(Route("POST", "/api/v2/service_accounts", _raw("service_account_write"), _ORG, body=g(_without_roles)))
    for method, path in (
        ("POST", "/api/v2/service_accounts/{service_account_id}/access_tokens"),
        ("PATCH", "/api/v2/service_accounts/{service_account_id}/access_tokens/{token_id}"),
        ("DELETE", "/api/v2/service_accounts/{service_account_id}/access_tokens/{token_id}"),
        ("POST", "/api/v2/service_accounts/{service_account_id}/application_keys"),
        ("PATCH", "/api/v2/service_accounts/{service_account_id}/application_keys/{app_key_id}"),
        ("DELETE", "/api/v2/service_accounts/{service_account_id}/application_keys/{app_key_id}"),
    ):
        out.append(Route(method, path, _raw("service_account_write"), _ORG))
    # Logs configuration (pipelines carry restriction policies: only creating one is routed).
    for method, path, perm in (
        ("PUT", "/api/v1/logs/config/index-order", "logs_modify_indexes"),
        ("POST", "/api/v1/logs/config/indexes", "logs_modify_indexes"),
        ("PUT", "/api/v1/logs/config/indexes/{name}", "logs_modify_indexes"),
        ("DELETE", "/api/v1/logs/config/indexes/{name}", "logs_modify_indexes"),
        ("POST", "/api/v1/logs/config/pipelines", "logs_write_pipelines"),
        ("PUT", "/api/v2/logs/config/archive-order", "logs_write_archives"),
        ("POST", "/api/v2/logs/config/archives", "logs_write_archives"),
        ("PUT", "/api/v2/logs/config/archives/{archive_id}", "logs_write_archives"),
        ("DELETE", "/api/v2/logs/config/archives/{archive_id}", "logs_write_archives"),
        ("POST", "/api/v2/logs/config/archives/{archive_id}/readers", "logs_write_archives"),
        ("DELETE", "/api/v2/logs/config/archives/{archive_id}/readers", "logs_write_archives"),
        ("DELETE", "/api/v2/logs/config/custom-destinations/{custom_destination_id}", "logs_write_forwarding_rules"),
        ("POST", "/api/v2/logs/config/metrics", "logs_generate_metrics"),
        ("PATCH", "/api/v2/logs/config/metrics/{metric_id}", "logs_generate_metrics"),
        ("DELETE", "/api/v2/logs/config/metrics/{metric_id}", "logs_generate_metrics"),
    ):
        out.append(Route(method, path, _raw(perm), _ORG))
    for method, path in (("POST", "/api/v2/logs/config/custom-destinations"), ("PATCH", "/api/v2/logs/config/custom-destinations/{custom_destination_id}")):
        out.append(Route(method, path, _raw("logs_write_forwarding_rules"), _ORG, sends_out=True, description="forwards logs to another destination"))
    for method, path in (
        ("POST", "/api/v2/logs/config/restriction_queries"),
        ("PATCH", "/api/v2/logs/config/restriction_queries/{restriction_query_id}"),
        ("PUT", "/api/v2/logs/config/restriction_queries/{restriction_query_id}"),
        ("DELETE", "/api/v2/logs/config/restriction_queries/{restriction_query_id}"),
        ("POST", "/api/v2/logs/config/restriction_queries/{restriction_query_id}/roles"),
        ("DELETE", "/api/v2/logs/config/restriction_queries/{restriction_query_id}/roles"),
    ):
        out.append(Route(method, path, um, _ORG))
    # Metrics configuration.
    for method, path, perm in (
        ("PUT", "/api/v1/metrics/{metric_name}", "metrics_metadata_write"),
        ("POST", "/api/v2/metrics/config/bulk-tags", "metric_tags_write"),
        ("DELETE", "/api/v2/metrics/config/bulk-tags", "metric_tags_write"),
        ("POST", "/api/v2/metrics/{metric_name}/tags", "metric_tags_write"),
        ("PATCH", "/api/v2/metrics/{metric_name}/tags", "metric_tags_write"),
        ("DELETE", "/api/v2/metrics/{metric_name}/tags", "metric_tags_write"),
        ("POST", "/api/v2/metrics/tag-indexing-rules", "metric_tags_write"),
        ("POST", "/api/v2/metrics/tag-indexing-rules/order", "metric_tags_write"),
        ("PUT", "/api/v2/metrics/tag-indexing-rules/{id}", "metric_tags_write"),
        ("DELETE", "/api/v2/metrics/tag-indexing-rules/{id}", "metric_tags_write"),
        ("POST", "/api/v2/metrics/{metric_name}/tag-indexing-rule-exemptions", "metric_tags_write"),
        ("DELETE", "/api/v2/metrics/{metric_name}/tag-indexing-rule-exemptions", "metric_tags_write"),
        ("POST", "/api/v2/metrics/derived-metrics", "metric_tags_write"),
        ("PUT", "/api/v2/metrics/derived-metrics/{metric_name}", "metric_tags_write"),
        ("DELETE", "/api/v2/metrics/derived-metrics/{metric_name}", "metric_tags_write"),
        ("POST", "/api/v2/metrics/historical-metrics-configurations", "late_metrics_config_write"),
        ("DELETE", "/api/v2/metrics/historical-metrics-configurations/{metric_name}", "late_metrics_config_write"),
    ):
        out.append(Route(method, path, _raw(perm), _ORG))
    # The keys' own validity: nothing about the user.
    out.append(Route("GET", "/api/v1/validate", None, "", untrusted_output=False, description="whether the API key is valid"))
    return out


def _locate(settings: Any) -> Sequence[tuple[str, str]]:
    """The connection's ``url`` (default https://api.datadoghq.com) and, for
    an ``api.<site>`` URL, the site's web host, which serves the same API,
    and its logs intake host."""
    get = getattr(settings, "get", None)
    url = (get("url") if callable(get) else "") or "https://" + DEFAULT_HOST
    ep = endpoint_of_url(url)
    if ep is None:
        return []
    host, path = ep
    out = [(host, path)]
    if host.startswith("api.") and ":" not in host:
        site = host[len("api.") :]
        web = "app." + site if site.count(".") == 1 else site  # app.datadoghq.com, us3.datadoghq.com
        out += [(web, path), ("http-intake.logs." + site, "")]
    return out


def _unrouted() -> dict[tuple[str, str], str]:
    out: dict[tuple[str, str], str] = {}

    def add(reason: str, *prefixes: str) -> None:
        for p in prefixes:
            out[("*", p)] = reason
            out[("*", p + "/**")] = reason

    add(
        "carries restriction policies the datadog integration does not read; it checks only monitors, dashboards, SLOs and notebooks",
        *_RESTRICTED,
    )
    add("the API description names no permission, or more than one, for these writes; toolpass checks one", *_NO_PERMISSION)
    add("not routed: these writes are refused until a route maps them to the permission they need", *_OTHER)
    out.update(_EXACT)
    return out


# Assets with restriction policies (other than the four the integration checks).
_RESTRICTED = (
    "/api/v1/synthetics",
    "/api/v2/synthetics",
    "/api/v1/integration/pagerduty",
    "/api/v1/integration/webhooks",
    "/api/v2/integration/opsgenie",
    "/api/v2/powerpacks",
    "/api/v2/reference-tables",
    "/api/v2/security_monitoring",
    "/api/v2/workflows",
    "/api/v2/app-builder",
    "/api/v2/actions",
    "/api/v2/rum",
    "/api/v2/on-call",
    "/api/v2/cases",
    "/api/v2/statuspages",
    "/api/v2/feature-flags",
    "/api/v1/logs/config/pipelines/{pipeline_id}",
    "/api/v2/monitor/notification_rule/{rule_id}",
    "/api/v2/org_connections",
)

# Writes whose permission the API description leaves out or splits.
_NO_PERMISSION = (
    "/api/v1/host",
    "/api/v1/tags",
    "/api/v1/org",
    "/api/v1/user/{user_handle}",
    "/api/v1/slo/correction",
    "/api/v2/dashboard/{dashboard_id}/shared",
    "/api/v1/dashboard/public",
    "/api/v2/team",
    "/api/v2/team-hierarchy-links",
    "/api/v2/tags",
    "/api/v2/logs/archive_searches",
)

# Every other product's API.
_OTHER = (
    "/api/unstable",
    "/api/v1/integration/aws",
    "/api/v1/integration/azure",
    "/api/v1/integration/gcp",
    "/api/v1/integration/slack",
    "/api/v1/security_analytics",
    "/api/v2/actions-datastores",
    "/api/v2/agentless_scanning",
    "/api/v2/ai_impact",
    "/api/v2/annotation",
    "/api/v2/anonymize_users",
    "/api/v2/apicatalog",
    "/api/v2/apm",
    "/api/v2/audit",
    "/api/v2/bits-ai",
    "/api/v2/catalog",
    "/api/v2/change-management",
    "/api/v2/ci",
    "/api/v2/cilogs",
    "/api/v2/cloud_auth",
    "/api/v2/cloud_security_management",
    "/api/v2/cloudinventoryservice",
    "/api/v2/code-coverage",
    "/api/v2/cost",
    "/api/v2/csm",
    "/api/v2/data-observability",
    "/api/v2/datasets",
    "/api/v2/ddsql",
    "/api/v2/deletion",
    "/api/v2/dem",
    "/api/v2/deployment_gates",
    "/api/v2/deployments",
    "/api/v2/domain_allowlist",
    "/api/v2/dora",
    "/api/v2/error-tracking",
    "/api/v2/experiments",
    "/api/v2/fleet",
    "/api/v2/forms",
    "/api/v2/governance",
    "/api/v2/hamr",
    "/api/v2/identity_providers",
    "/api/v2/idp",
    "/api/v2/incidents",
    "/api/v2/integration",
    "/api/v2/integration-interfaces",
    "/api/v2/integrations",
    "/api/v2/ip_allowlist",
    "/api/v2/llm-obs",
    "/api/v2/login",
    "/api/v2/maintenance_windows",
    "/api/v2/model-lab-api",
    "/api/v2/ndm",
    "/api/v2/oauth2",
    "/api/v2/obs-pipelines",
    "/api/v2/org",
    "/api/v2/org_authorized_clients",
    "/api/v2/org_configs",
    "/api/v2/org_group_memberships",
    "/api/v2/org_group_policies",
    "/api/v2/org_group_policy_overrides",
    "/api/v2/org_groups",
    "/api/v2/personal_access_tokens",
    "/api/v2/prodlytics",
    "/api/v2/product-analytics",
    "/api/v2/remote_config",
    "/api/v2/replay",
    "/api/v2/reporting",
    "/api/v2/saml_configurations",
    "/api/v2/scorecard",
    "/api/v2/seats",
    "/api/v2/security",
    "/api/v2/sensitive-data-scanner",
    "/api/v2/services",
    "/api/v2/siem-historical-detections",
    "/api/v2/snapshot",
    "/api/v2/sourcemaps",
    "/api/v2/spa",
    "/api/v2/static-analysis",
    "/api/v2/static-analysis-sca",
    "/api/v2/stegadography",
    "/api/v2/test",
    "/api/v2/timeseries-anomaly-investigations",
    "/api/v2/usage",
    "/api/v2/user_authorized_clients",
    "/api/v2/web-integrations",
    "/api/v2/widgets",
)

_INTAKE = "data intake authenticates with the API key alone; no user permission governs it"
_OWN_KEYS = "acts on the keys and profile of the user whose application key the agent holds, not the session's user"
_EXACT: dict[tuple[str, str], str] = {
    ("POST", "/api/v1/series"): _INTAKE,
    ("POST", "/api/v1/check_run"): _INTAKE,
    ("POST", "/api/v1/distribution_points"): _INTAKE,
    ("POST", "/api/v1/events"): _INTAKE,
    ("POST", "/api/v2/series"): _INTAKE,
    ("POST", "/api/v2/events"): _INTAKE,
    ("POST", "/api/v2/logs"): _INTAKE + " (served on the logs intake host)",
    ("POST", "/v1/input"): _INTAKE + " (served on the logs intake host)",
    ("POST", "/api/v1/application_key"): _OWN_KEYS,
    ("*", "/api/v2/current_user"): _OWN_KEYS,
    ("*", "/api/v2/current_user/**"): _OWN_KEYS,
    ("PUT", "/api/v1/logs/config/pipeline-order"): "reorders every pipeline, including restricted ones the integration does not read",
}

ROUTES = RouteTable(
    "datadog",
    _routes(),
    claims=("/api", "/v1"),
    unrouted=_unrouted(),
    locate=_locate,
)

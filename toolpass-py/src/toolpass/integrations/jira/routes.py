"""Jira Cloud REST requests (``/rest/api/3``), mapped to the jira
integration's actions: Jira's own permission keys, on the project or issue
the request touches.

The issue comes from the path and must be a key (``PAY-123``): Jira answers
permission checks on keys, so a numeric issue id is left unresolved and the
request refused. Creating an issue checks ``CREATE_ISSUES`` on the project
named in the body (``fields.project.key``). A bulk request is checked when
every issue in it belongs to one project, and left unresolved otherwise.

Writes outside issues and projects, and bulk changes selected by JQL, are
not routed: a write with no route is refused.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from toolpass.core.routes import Request, Route, RouteTable

__all__ = ["ROUTES"]

_ISSUE_KEY = re.compile(r"[A-Z][A-Z0-9_]{1,9}-[1-9][0-9]*")
_PROJECT_KEY = re.compile(r"[A-Z][A-Z0-9_]{1,9}")
_ISSUE = "issue:{issueIdOrKey}"
_PROJECT = "project:{projectIdOrKey}"
_A = "/rest/api/3"


def _issue(r: Request, vars_: Mapping[str, str]) -> dict[str, str]:
    """The issue in the path must be a key; Jira's permission check takes keys."""
    key = vars_.get("issueIdOrKey", "")
    if not _ISSUE_KEY.fullmatch(key):
        return {"_unresolved": f"the issue {key!r} is not named by its key (PAY-123); toolpass checks issues by key"}
    return {}


def _project_path(r: Request, vars_: Mapping[str, str]) -> dict[str, str]:
    key = vars_.get("projectIdOrKey", "")
    if not _PROJECT_KEY.fullmatch(key):
        return {"_unresolved": f"the project {key!r} is not named by its key (PAY); toolpass checks projects by key"}
    return {}


def _project_of(fields: Any) -> str | None:
    if not isinstance(fields, Mapping):
        return None
    project = fields.get("project")
    if isinstance(project, Mapping):
        key = project.get("key")
        if isinstance(key, str) and _PROJECT_KEY.fullmatch(key):
            return key
    return None


def _create(r: Request, vars_: Mapping[str, str]) -> dict[str, str]:
    """POST /issue: the project from the body's fields.project.key."""
    body = r.json()
    key = _project_of(body.get("fields") if isinstance(body, Mapping) else None)
    if key is None:
        return {"_unresolved": "the request does not name the project by its key (fields.project.key)"}
    return {"projectIdOrKey": key}


def _create_bulk(r: Request, vars_: Mapping[str, str]) -> dict[str, str]:
    """POST /issue/bulk: one project for every issue in it."""
    body = r.json()
    updates = body.get("issueUpdates") if isinstance(body, Mapping) else None
    keys = {_project_of(u.get("fields")) if isinstance(u, Mapping) else None for u in updates} if isinstance(updates, list) and updates else {None}
    if None in keys or len(keys) != 1:
        return {"_unresolved": "a bulk create is checked only when every issue names the same project by its key"}
    return {"projectIdOrKey": keys.pop() or ""}


def _fetch_bulk(r: Request, vars_: Mapping[str, str]) -> dict[str, str]:
    """POST /issue/bulkfetch: a read of issues all in one project."""
    body = r.json()
    ids = body.get("issueIdsOrKeys") if isinstance(body, Mapping) else None
    if not isinstance(ids, list) or not ids or not all(isinstance(i, str) and _ISSUE_KEY.fullmatch(i) for i in ids):
        return {"_unresolved": "a bulk fetch is checked only when it names issues by key"}
    projects = {i.rsplit("-", 1)[0] for i in ids}
    if len(projects) != 1:
        return {"_unresolved": "a bulk fetch is checked only when every issue is in one project"}
    return {"projectIdOrKey": projects.pop()}


def _issue_route(method: str, path: str, action: str, **kw: Any) -> Route:
    return Route(method, f"{_A}/issue/{{issueIdOrKey}}{path}", action, _ISSUE, body=_issue, **kw)


def _routes() -> list[Route]:
    b, edit, work = "BROWSE_PROJECTS", "EDIT_ISSUES", "WORK_ON_ISSUES"
    return [
        # Reads.
        _issue_route("GET", "", b),
        _issue_route("GET", "/{rest+}", b, spec=False),
        Route("GET", f"{_A}/project/{{projectIdOrKey}}", b, _PROJECT, body=_project_path),
        Route("GET", f"{_A}/project/{{projectIdOrKey}}/{{rest+}}", b, _PROJECT, body=_project_path, spec=False),
        Route("GET", f"{_A}/myself", None, "", untrusted_output=False, description="the agent's own account"),
        Route("GET", f"{_A}/serverInfo", None, "", untrusted_output=False),
        Route("GET", f"{_A}/search/jql", None, "", description="JQL search: Jira filters it by the agent's own access; results are untrusted"),
        Route("POST", f"{_A}/search/jql", None, "", effect="read", description="JQL search"),
        Route("POST", f"{_A}/issue/bulkfetch", b, _PROJECT, effect="read", body=_fetch_bulk),
        Route("POST", f"{_A}/issue/watching", None, "", effect="read", description="whether the agent watches some issues"),
        _issue_route("POST", "/changelog/list", b, effect="read"),
        # Creating issues.
        Route("POST", f"{_A}/issue", "CREATE_ISSUES", _PROJECT, body=_create),
        Route("POST", f"{_A}/issue/bulk", "CREATE_ISSUES", _PROJECT, body=_create_bulk),
        # One issue.
        _issue_route("PUT", "", edit),
        _issue_route("DELETE", "", "DELETE_ISSUES"),
        _issue_route("PUT", "/assignee", "ASSIGN_ISSUES"),
        _issue_route("POST", "/transitions", "TRANSITION_ISSUES"),
        _issue_route("POST", "/attachments", "CREATE_ATTACHMENTS"),
        _issue_route("POST", "/comment", "ADD_COMMENTS"),
        _issue_route("PUT", "/comment/{id}", "EDIT_ALL_COMMENTS"),
        _issue_route("DELETE", "/comment/{id}", "DELETE_ALL_COMMENTS"),
        _issue_route("POST", "/notify", b, sends_out=True, description="sends an email about the issue"),
        _issue_route("PUT", "/properties/{propertyKey}", edit),
        _issue_route("DELETE", "/properties/{propertyKey}", edit),
        _issue_route("POST", "/remotelink", "LINK_ISSUES"),
        _issue_route("DELETE", "/remotelink", "LINK_ISSUES"),
        _issue_route("PUT", "/remotelink/{linkId}", "LINK_ISSUES"),
        _issue_route("DELETE", "/remotelink/{linkId}", "LINK_ISSUES"),
        _issue_route("POST", "/votes", b),
        _issue_route("DELETE", "/votes", b),
        _issue_route("POST", "/watchers", "MANAGE_WATCHERS"),
        _issue_route("DELETE", "/watchers", "MANAGE_WATCHERS"),
        _issue_route("POST", "/worklog", work),
        _issue_route("DELETE", "/worklog", work),
        _issue_route("POST", "/worklog/move", work),
        _issue_route("PUT", "/worklog/{id}", work),
        _issue_route("DELETE", "/worklog/{id}", work),
        _issue_route("PUT", "/worklog/{worklogId}/properties/{propertyKey}", work),
        _issue_route("DELETE", "/worklog/{worklogId}/properties/{propertyKey}", work),
    ]


ROUTES = RouteTable(
    "jira",
    _routes(),
    claims=(f"{_A}/issue",),
    unrouted={
        ("POST", f"{_A}/issue/archive"): "archives the issues a JQL query selects: not checkable issue by issue",
        ("PUT", f"{_A}/issue/archive"): "archiving is a Jira administrator's job, done outside agents",
        ("PUT", f"{_A}/issue/unarchive"): "unarchiving is a Jira administrator's job, done outside agents",
        ("POST", f"{_A}/issue/properties"): "sets a property on the issues a query selects: not checkable issue by issue",
        ("POST", f"{_A}/issue/properties/multi"): "sets properties across issues: not checkable issue by issue",
        ("PUT", f"{_A}/issue/properties/{{propertyKey}}"): "sets a property on the issues a filter selects: not checkable issue by issue",
        ("DELETE", f"{_A}/issue/properties/{{propertyKey}}"): "deletes a property from the issues a filter selects: not checkable issue by issue",
    },
)

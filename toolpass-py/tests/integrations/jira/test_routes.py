"""The jira route table against Jira Cloud's REST API description, and its shape."""

from __future__ import annotations

import json

import pytest

from tests.harness.routes import check_routes_against_spec
from toolpass.core.integration import Settings
from toolpass.core.routes import Request
from toolpass.integrations.jira.actions import ACTIONS
from toolpass.integrations.jira.routes import ROUTES


def req(method: str, path: str, body: dict | None = None) -> Request:
    return Request("https", "acme.atlassian.net", 443, method, path, "", {"content-type": "application/json"}, json.dumps(body).encode() if body else None)


def test_routes_match_the_api_description():
    assert check_routes_against_spec(ROUTES, "jira", host="acme.atlassian.net") == []


def test_every_route_names_a_known_action():
    for r in ROUTES.routes:
        assert r.action is None or r.action in ACTIONS, r


@pytest.mark.parametrize(
    ("method", "path", "body", "action", "resource", "effect"),
    [
        ("GET", "/rest/api/3/issue/PAY-12", None, "BROWSE_PROJECTS", "issue:PAY-12", "read"),
        ("GET", "/rest/api/3/issue/PAY-12/comment", None, "BROWSE_PROJECTS", "issue:PAY-12", "read"),
        ("GET", "/rest/api/3/project/PAY", None, "BROWSE_PROJECTS", "project:PAY", "read"),
        ("POST", "/rest/api/3/issue", {"fields": {"project": {"key": "PAY"}, "summary": "x"}}, "CREATE_ISSUES", "project:PAY", "write"),
        (
            "POST",
            "/rest/api/3/issue/bulk",
            {"issueUpdates": [{"fields": {"project": {"key": "OPS"}}}, {"fields": {"project": {"key": "OPS"}}}]},
            "CREATE_ISSUES",
            "project:OPS",
            "write",
        ),
        ("POST", "/rest/api/3/issue/bulkfetch", {"issueIdsOrKeys": ["PAY-1", "PAY-2"]}, "BROWSE_PROJECTS", "project:PAY", "read"),
        ("PUT", "/rest/api/3/issue/PAY-12", {"fields": {"summary": "y"}}, "EDIT_ISSUES", "issue:PAY-12", "write"),
        ("DELETE", "/rest/api/3/issue/PAY-12", None, "DELETE_ISSUES", "issue:PAY-12", "destructive"),
        ("PUT", "/rest/api/3/issue/PAY-12/assignee", {"accountId": "x"}, "ASSIGN_ISSUES", "issue:PAY-12", "write"),
        ("POST", "/rest/api/3/issue/PAY-12/transitions", {"transition": {"id": "31"}}, "TRANSITION_ISSUES", "issue:PAY-12", "write"),
        ("POST", "/rest/api/3/issue/PAY-12/comment", {"body": "hi"}, "ADD_COMMENTS", "issue:PAY-12", "write"),
        ("DELETE", "/rest/api/3/issue/PAY-12/comment/100", None, "DELETE_ALL_COMMENTS", "issue:PAY-12", "destructive"),
        ("POST", "/rest/api/3/issue/PAY-12/worklog", {"timeSpent": "1h"}, "WORK_ON_ISSUES", "issue:PAY-12", "write"),
        ("POST", "/rest/api/3/issue/PAY-12/watchers", None, "MANAGE_WATCHERS", "issue:PAY-12", "write"),
        ("GET", "/rest/api/3/myself", None, None, "", "read"),
        ("GET", "/rest/api/3/issue/createmeta", None, None, "", "read"),
        ("GET", "/rest/api/3/issue/createmeta/PAY/issuetypes", None, "BROWSE_PROJECTS", "project:PAY", "read"),
        ("GET", "/rest/api/3/issue/picker", None, None, "", "read"),
        ("POST", "/rest/api/3/search/jql", {"jql": "project = PAY"}, None, "", "read"),
    ],
)
def test_routing(method, path, body, action, resource, effect):
    o = ROUTES.match(req(method, path, body))
    assert o is not None and o.unresolved is None, o
    assert (o.action, o.resource, o.effect) == (action, resource, effect)


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("PUT", "/rest/api/3/issue/10042", {}),  # a numeric id: Jira checks keys
        ("POST", "/rest/api/3/issue", {"fields": {"project": {"id": "10000"}}}),  # project by id only
        ("POST", "/rest/api/3/issue", {"fields": {"summary": "no project"}}),
        ("POST", "/rest/api/3/issue/bulk", {"issueUpdates": [{"fields": {"project": {"key": "PAY"}}}, {"fields": {"project": {"key": "OPS"}}}]}),
        ("POST", "/rest/api/3/issue/bulkfetch", {"issueIdsOrKeys": ["PAY-1", "OPS-2"]}),
        ("GET", "/rest/api/3/project/10000", None),
        ("POST", "/rest/api/3/issue", {"fields": {"project": {"key": "PAY", "id": "10042"}}}),  # Jira would use the id
        ("POST", "/rest/api/3/issue", {"fields": {"project": {"key": "PAY"}}, "update": {"project": [{"set": {"id": "10042"}}]}}),
        ("POST", "/rest/api/3/issue/bulk", {"issueUpdates": [{"fields": {"project": {"key": "PAY", "name": "Secret"}}}]}),
    ],
)
def test_requests_that_do_not_name_one_key_are_unresolved(method, path, body):
    o = ROUTES.match(req(method, path, body))
    assert o is not None and o.unresolved, o


def test_notify_sends_out_and_reads_are_untrusted():
    assert ROUTES.match(req("POST", "/rest/api/3/issue/PAY-1/notify", {})).sends_out  # type: ignore[union-attr]
    assert ROUTES.match(req("GET", "/rest/api/3/issue/PAY-1")).untrusted_output  # type: ignore[union-attr]


def test_unrouted_writes_have_no_route():
    assert ROUTES.match(req("POST", "/rest/api/3/issue/archive", {"jql": "x"})) is None
    assert ROUTES.is_unrouted("POST", "/rest/api/3/issue/archive")
    assert ROUTES.match(req("POST", "/rest/api/3/project", {"key": "NEW"})) is None, "a write outside issues is refused at the wire"


def test_the_site_is_where_the_api_lives():
    assert ROUTES.endpoints(Settings("jira-main", "jira", {"url": "https://acme.atlassian.net"})) == [("acme.atlassian.net", "")]


def test_moving_worklogs_into_another_issue_is_not_routed():
    path = "/rest/api/3/issue/PAY-1/worklog/move"
    assert ROUTES.match(req("POST", path, {"ids": [1], "issueIdOrKey": "SECRET-7"})) is None
    assert ROUTES.is_unrouted("POST", path)

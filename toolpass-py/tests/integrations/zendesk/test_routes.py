"""The zendesk route table against the Support API description, and its shape."""

from __future__ import annotations

import json
from typing import Any

import pytest

from tests.harness.routes import check_routes_against_spec
from tests.integrations.zendesk.test_zendesk import Setup, admin, check, dana, setup, setup_mode  # noqa: F401
from toolpass.core.catalog import parse_resource
from toolpass.core.decision import Code
from toolpass.core.integration import Settings, find_action
from toolpass.core.routes import Request
from toolpass.integrations.zendesk import INTEGRATION
from toolpass.integrations.zendesk.actions import parse_target
from toolpass.integrations.zendesk.routes import ROUTES

JSON = {"content-type": "application/json"}


def req(method: str, path: str, body: Any = None, query: str = "", headers: dict[str, str] | None = None) -> Request:
    raw = body if isinstance(body, bytes) else (json.dumps(body).encode() if body is not None else None)
    return Request("https", "acme.zendesk.com", 443, method, path, query, JSON if headers is None else headers, raw)


def test_routes_match_the_api_description():
    assert check_routes_against_spec(ROUTES, "zendesk", host="acme.zendesk.com") == []


def test_every_route_names_a_known_action():
    for r in ROUTES.routes:
        assert r.action is None or find_action(INTEGRATION, r.action) is not None, r


def test_the_account_url_is_where_the_api_lives():
    assert ROUTES.endpoints(Settings("zd", "zendesk", {"url": "https://acme.zendesk.com"})) == [("acme.zendesk.com", "")]


ROUTED = [
    ("GET", "/api/v2/tickets/12", None, "ticket.view", "ticket:12", "read"),
    ("GET", "/api/v2/tickets/12.json", None, "ticket.view", "ticket:12", "read"),
    ("GET", "/api/v2/tickets/12/comments", None, "ticket.view", "ticket:12", "read"),
    ("GET", "/api/v2/requests/12/comments", None, "ticket.view", "ticket:12", "read"),
    ("GET", "/api/v2/tickets/recent", None, None, "", "read"),
    ("GET", "/api/v2/search.json", None, None, "", "read"),
    ("GET", "/api/v2/users/me.json", None, None, "", "read"),
    ("POST", "/api/v2/views/preview", {"view": {}}, None, "", "read"),
    # A ticket update: properties, an internal note, a public comment.
    ("PUT", "/api/v2/tickets/12", {"ticket": {"status": "solved", "assignee_id": 7}}, "ticket.edit", "ticket:12", "write"),
    ("PUT", "/api/v2/tickets/12.json", {"ticket": {"priority": "high"}}, "ticket.edit", "ticket:12", "write"),
    ("PUT", "/api/v2/tickets/12", {"ticket": {"comment": {"body": "note", "public": False}, "status": "pending"}}, "ticket.edit", "ticket:12", "write"),
    ("PUT", "/api/v2/tickets/12", {"ticket": {"comment": {"body": "hi", "public": True}}}, "ticket.comment_public", "ticket:12", "write"),
    (
        "PUT",
        "/api/v2/tickets/12",
        {"ticket": {"comment": {"body": "hi"}, "safe_update": True, "updated_stamp": "x"}},
        "ticket.comment_public",
        "ticket:12",
        "write",
    ),
    ("PUT", "/api/v2/requests/12", {"request": {"comment": {"body": "thanks"}}}, "ticket.comment_public", "ticket:12", "write"),
    ("PUT", "/api/v2/requests/12", {"request": {"solved": True}}, "ticket.edit", "ticket:12", "write"),
    ("DELETE", "/api/v2/tickets/12", None, "ticket.delete", "ticket:12", "destructive"),
    ("PUT", "/api/v2/tickets/12/tags", {"tags": ["vip"]}, "ticket.edit", "ticket:12", "write"),
    ("PUT", "/api/v2/tickets/12/comments/99/redact", {"text": "secret"}, "ticket.delete", "ticket:12", "destructive"),
    ("PUT", "/api/v2/tickets/12/comments/99/make_private", None, "ticket.edit", "ticket:12", "write"),
    # Organizations.
    ("PUT", "/api/v2/organizations/500", {"organization": {"notes": "x"}}, "organization.edit", "organization:500", "write"),
    ("DELETE", "/api/v2/organizations/500", None, "organization.edit", "organization:500", "destructive"),
    ("POST", "/api/v2/organizations", {"organization": {"name": "New"}}, "account.admin", "account", "write"),
    # Users: the profile, or more.
    ("PUT", "/api/v2/users/20", {"user": {"name": "Ann", "phone": "+1", "user_fields": {"tier": "gold"}}}, "user.edit", "user:20", "write"),
    ("PUT", "/api/v2/users/20", {"user": {"role": "admin"}}, "account.admin", "account", "write"),
    ("PUT", "/api/v2/users/20", {"user": {"name": "Ann", "organization_id": 9}}, "account.admin", "account", "write"),
    ("PUT", "/api/v2/users/20", {"user": {"suspended": True}}, "account.admin", "account", "write"),
    ("POST", "/api/v2/users/20/identities", {"identity": {"type": "email", "value": "a@b.c"}}, "user.edit", "user:20", "write"),
    ("DELETE", "/api/v2/users/20", None, "account.admin", "account", "destructive"),
    ("POST", "/api/v2/users/20/group_memberships", {"group_membership": {"group_id": 3}}, "account.admin", "account", "write"),
    # Account configuration.
    ("POST", "/api/v2/macros", {"macro": {}}, "macro.manage", "account", "write"),
    ("PUT", "/api/v2/macros/5.json", {"macro": {}}, "macro.manage", "account", "write"),
    ("DELETE", "/api/v2/views/5", None, "view.manage", "account", "destructive"),
    ("PUT", "/api/v2/triggers/reorder", {"trigger_ids": [1]}, "business_rules.manage", "account", "write"),
    ("POST", "/api/v2/automations", {"automation": {}}, "business_rules.manage", "account", "write"),
    ("POST", "/api/v2/custom_objects/car/triggers", {"trigger": {}}, "business_rules.manage", "account", "write"),
    ("POST", "/api/v2/ticket_fields", {"ticket_field": {}}, "account.admin", "account", "write"),
    ("PUT", "/api/v2/groups/3", {"group": {}}, "account.admin", "account", "write"),
    ("POST", "/api/v2/targets", {"target": {}}, "account.admin", "account", "write"),
    ("POST", "/api/v2/uploads.json", b"file", None, "", "write"),
]


@pytest.mark.parametrize(("method", "path", "body", "action", "resource", "effect"), ROUTED)
def test_routing(method, path, body, action, resource, effect):
    o = ROUTES.match(req(method, path, body))
    assert o is not None and o.unresolved is None, o
    assert (o.action, o.resource, o.effect) == (action, resource, effect)


def test_resources_parse_as_the_integration_parses_them():
    for method, path, body, action, _, _ in ROUTED:
        if action is None:
            continue
        o = ROUTES.match(req(method, path, body))
        assert o is not None and o.action is not None and o.resource is not None
        parse_target(o.action, parse_resource(o.resource))  # raises on a resource the check would refuse


def test_the_check_answers_the_routed_questions(setup: Setup):  # noqa: F811
    _, _, c = setup()
    for method, path, body in [
        ("PUT", "/api/v2/tickets/1", {"ticket": {"comment": {"body": "hi"}}}),
        ("PUT", "/api/v2/tickets/1.json", {"ticket": {"status": "pending"}}),
        ("DELETE", "/api/v2/tickets/2", None),
        ("PUT", "/api/v2/organizations/500", {"organization": {"notes": "x"}}),
        ("PUT", "/api/v2/users/20", {"user": {"name": "Ann"}}),
        ("PUT", "/api/v2/users/20", {"user": {"role": "admin"}}),
        ("POST", "/api/v2/macros", {"macro": {}}),
    ]:
        o = ROUTES.match(req(method, path, body))
        assert o is not None and o.action is not None and o.resource is not None and o.unresolved is None, (path, o)
        d = check(c, admin, o.action, o.resource)
        assert d.code == Code.ALLOWED, (path, o.action, o.resource, d.text)
    # A role change is an administrator's even for an agent who may edit end-user profiles.
    o = ROUTES.match(req("PUT", "/api/v2/users/20", {"user": {"role": "admin"}}))
    assert o is not None and check(c, dana, o.action or "", o.resource or "").code == Code.DENIED
    o = ROUTES.match(req("PUT", "/api/v2/users/20", {"user": {"name": "Ann"}}))
    assert o is not None and check(c, dana, o.action or "", o.resource or "").code == Code.ALLOWED


@pytest.mark.parametrize(
    ("method", "path", "body", "query", "headers"),
    [
        ("PUT", "/api/v2/tickets/0012", {"ticket": {}}, "", None),  # not a plain decimal id
        ("PUT", "/api/v2/tickets/update_many", {"tickets": []}, "ids=1,2", None),
        ("DELETE", "/api/v2/tickets/destroy_many", None, "ids=1,2", None),
        ("GET", "/api/v2/tickets/12.JSON", None, "", None),
        # A public comment beside property changes needs two checks.
        ("PUT", "/api/v2/tickets/12", {"ticket": {"comment": {"body": "hi"}, "status": "solved"}}, "", None),
        ("PUT", "/api/v2/requests/12", {"request": {"comment": {"body": "hi"}, "solved": True}}, "", None),
        # Visibility named some other way.
        ("PUT", "/api/v2/tickets/12", {"ticket": {"comment": {"body": "hi", "public": "false"}}}, "", None),
        ("PUT", "/api/v2/tickets/12", {"ticket": {"comment": {"body": "hi", "public": False, "is_public": True}}}, "", None),
        ("PUT", "/api/v2/tickets/12", {"ticket": {"comment": "hi"}}, "", None),
        ("PUT", "/api/v2/tickets/12", {"ticket": {"comment": {"body": "hi", "author_id": 20}}}, "", None),  # posted as someone else
        # The body names another record, or is not read exactly.
        ("PUT", "/api/v2/tickets/12", {"ticket": {"id": 13, "status": "open"}}, "", None),
        ("PUT", "/api/v2/tickets/12", {"status": "open"}, "", None),
        ("PUT", "/api/v2/tickets/12", {"ticket": {}, "system_metadata": {}}, "", None),
        ("PUT", "/api/v2/tickets/12", [{"ticket": {}}], "", None),
        ("PUT", "/api/v2/tickets/12", b'{"ticket": {"comment": {"public": false}}, "ticket": {"comment": {"public": true}}}', "", None),
        ("PUT", "/api/v2/tickets/12", b'{"ticket": {"comment": {"body": "x", "public": false, "public": true}}}', "", None),
        ("PUT", "/api/v2/tickets/12", b"ticket[comment][body]=hi", "", {"content-type": "application/x-www-form-urlencoded"}),
        ("PUT", "/api/v2/tickets/12", b'{"ticket": {}}', "", {"content-type": "text/plain"}),
        ("PUT", "/api/v2/tickets/12", {"ticket": {"comment": {"body": "x", "public": False}}}, "ticket[comment][public]=true", None),
        ("PUT", "/api/v2/users/20", {"user": {"name": "Ann"}}, "user[role]=admin", None),
        ("PUT", "/api/v2/users/20", {"user": {"id": 21, "name": "Ann"}}, "", None),
        ("PUT", "/api/v2/users/me", {"user": {"name": "Ann"}}, "", None),
        ("PUT", "/api/v2/organizations/500", {"organization": {"id": 501}}, "", None),
        ("PUT", "/api/v2/tickets/12", b'{"ticket": {"priority": NaN}}', "", None),
    ],
)
def test_requests_that_cannot_be_read_exactly_are_unresolved(method, path, body, query, headers):
    o = ROUTES.match(req(method, path, body, query, headers))
    assert o is not None and o.unresolved, o


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/api/v2/tickets"),
        ("POST", "/api/v2/tickets.json"),
        ("POST", "/api/v2/tickets/12/merge"),
        ("PUT", "/api/v2/tickets/12/mark_as_spam"),
        ("POST", "/api/v2/requests"),
        ("DELETE", "/api/v2/attachments/9"),
        ("PUT", "/api/v2/users/20/password"),
        ("POST", "/api/v2/bookmarks"),
        ("PUT", "/api/v2/task_lists/4"),
    ],
)
def test_unrouted_writes_have_no_route(method, path):
    assert ROUTES.match(req(method, path, {})) is None
    assert ROUTES.is_unrouted(method, path) or path.endswith(".json")


def test_ticket_updates_send_out_and_reads_are_untrusted():
    assert ROUTES.match(req("PUT", "/api/v2/tickets/1", {"ticket": {"status": "open"}})).sends_out  # type: ignore[union-attr]
    assert ROUTES.match(req("GET", "/api/v2/tickets/1")).untrusted_output  # type: ignore[union-attr]
    assert not ROUTES.match(req("GET", "/api/v2/users/me")).untrusted_output  # type: ignore[union-attr]

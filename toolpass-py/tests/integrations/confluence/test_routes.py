"""The confluence route table against Confluence Cloud's v1 and v2 REST API
descriptions, and its shape."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import pytest

from tests.harness.routes import check_routes_against_spec
from tests.integrations.confluence.test_confluence import FakeSite, Setup, check, dana, servers, setup  # noqa: F401
from toolpass.core.catalog import parse_resource
from toolpass.core.decision import Code
from toolpass.core.integration import Settings, find_action
from toolpass.core.routes import Request, RouteTable
from toolpass.integrations.confluence import INTEGRATION
from toolpass.integrations.confluence.actions import ACTIONS
from toolpass.integrations.confluence.actions import parse_resource as parse_confluence
from toolpass.integrations.confluence.routes import ROUTES, V1, V1_CLAIMS, V1_UNROUTED, V2, V2_UNROUTED

JSON = {"content-type": "application/json"}


def req(method: str, path: str, body: Any = None, query: str = "", headers: dict[str, str] | None = None) -> Request:
    raw = body if isinstance(body, bytes) else (json.dumps(body).encode() if body is not None else None)
    return Request("https", "acme.atlassian.net", 443, method, path, query, JSON if headers is None else headers, raw)


def _strip(path: str) -> str:
    return path[len(V2) :] or "/"


def test_v1_routes_match_the_v1_description():
    v1 = RouteTable("confluence", [r for r in ROUTES.routes if r.path.startswith(V1 + "/")], claims=V1_CLAIMS, unrouted=V1_UNROUTED)
    assert check_routes_against_spec(v1, "confluence-v1", host="acme.atlassian.net") == []


def test_v2_routes_match_the_v2_description():
    # The v2 description's paths are relative to /wiki/api/v2.
    v2 = RouteTable(
        "confluence",
        [replace(r, path=_strip(r.path)) for r in ROUTES.routes if r.path.startswith(V2 + "/")],
        claims=("/",),
        unrouted={(m, _strip(p)): why for (m, p), why in V2_UNROUTED.items()},
    )
    assert check_routes_against_spec(v2, "confluence-v2", host="acme.atlassian.net") == []


def test_every_route_is_v1_or_v2_and_names_a_known_action():
    for r in ROUTES.routes:
        assert r.path.startswith((V1 + "/", V2 + "/")), r
        assert r.action is None or find_action(INTEGRATION, r.action) is not None, r


def test_the_site_is_where_the_api_lives():
    assert ROUTES.endpoints(Settings("conf", "confluence", {"url": "https://acme.atlassian.net"})) == [("acme.atlassian.net", "")]


ROUTED = [
    ("GET", "/wiki/api/v2/pages/100", None, "page.read", "page:100", "read"),
    ("GET", "/wiki/api/v2/pages/100/footer-comments", None, "page.read", "page:100", "read"),
    ("GET", "/wiki/api/v2/blogposts/200/versions/3", None, "blogpost.read", "blogpost:200", "read"),
    (
        "PUT",
        "/wiki/api/v2/pages/100",
        {"id": "100", "status": "current", "title": "T", "spaceId": "98307", "body": {}, "version": {"number": 2}},
        "page.update",
        "page:100",
        "write",
    ),
    (
        "PUT",
        "/wiki/api/v2/blogposts/200",
        {"id": 200, "status": "current", "title": "T", "body": {}, "version": {"number": 2}},
        "blogpost.update",
        "blogpost:200",
        "write",
    ),
    ("PUT", "/wiki/api/v2/pages/100/title", {"status": "current", "title": "New"}, "page.update", "page:100", "write"),
    ("DELETE", "/wiki/api/v2/pages/100", None, "page.delete", "page:100", "destructive"),
    ("DELETE", "/wiki/api/v2/blogposts/200", None, "blogpost.delete", "blogpost:200", "destructive"),
    ("POST", "/wiki/api/v2/pages/100/properties", {"key": "k", "value": 1}, "page.update", "page:100", "write"),
    ("DELETE", "/wiki/api/v2/blogposts/200/properties/7", None, "blogpost.update", "blogpost:200", "destructive"),
    ("PUT", "/wiki/api/v2/pages/100/classification-level", {"id": "c1", "status": "current"}, "page.update", "page:100", "write"),
    ("POST", "/wiki/api/v2/content/convert-ids-to-types", {"contentIds": ["100"]}, None, "", "read"),
    # v1.
    ("POST", "/wiki/rest/api/content", {"type": "page", "title": "T", "space": {"key": "DEV"}, "body": {}}, "page.create", "space:DEV", "write"),
    ("POST", "/wiki/rest/api/content", {"type": "blogpost", "title": "T", "space": {"key": "DEV"}, "ancestors": []}, "blogpost.create", "space:DEV", "write"),
    ("GET", "/wiki/rest/api/content/100", None, "page.read", "page:100", "read"),
    ("GET", "/wiki/rest/api/content/100/child/page", None, "page.read", "page:100", "read"),
    ("GET", "/wiki/rest/api/content/search", None, None, "", "read"),
    ("GET", "/wiki/rest/api/search", None, None, "", "read"),
    ("GET", "/wiki/rest/api/user/current", None, None, "", "read"),
    ("POST", "/wiki/rest/api/content/100/permission/check", {"subject": {}, "operation": "read"}, None, "", "read"),
    ("GET", "/wiki/rest/api/space/DEV", None, "space.read", "space:DEV", "read"),
    ("PUT", "/wiki/rest/api/space/DEV", {"name": "Dev"}, "space.admin", "space:DEV", "write"),
    ("DELETE", "/wiki/rest/api/space/DEV", None, "space.admin", "space:DEV", "destructive"),
    ("POST", "/wiki/rest/api/space/DEV/permission", {}, "space.admin", "space:DEV", "write"),
    ("PUT", "/wiki/rest/api/space/~acc-dana/settings", {}, "space.admin", "space:~acc-dana", "write"),
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
        res = parse_confluence(parse_resource(o.resource))  # raises on a resource the check would refuse
        assert res.kind == ACTIONS[o.action].resource, (o.action, o.resource)


def test_the_check_answers_the_routed_questions(setup: Setup):  # noqa: F811
    _, c = setup(FakeSite(), True)
    for method, path, body in [
        ("PUT", "/wiki/api/v2/pages/100", {"id": "100", "status": "current", "title": "T", "body": {}, "version": {"number": 2}}),
        ("DELETE", "/wiki/api/v2/blogposts/200", None),
        ("GET", "/wiki/rest/api/content/100", None),
        ("POST", "/wiki/rest/api/content", {"type": "page", "title": "T", "space": {"key": "DEV"}}),
        ("PUT", "/wiki/rest/api/space/DEV", {"name": "Dev"}),
    ]:
        o = ROUTES.match(req(method, path, body))
        assert o is not None and o.action is not None and o.resource is not None and o.unresolved is None, (path, o)
        d = check(c, dana, o.action, o.resource)
        assert d.code == Code.ALLOWED, (path, o.action, o.resource, d.text)


@pytest.mark.parametrize(
    ("method", "path", "body", "query", "headers"),
    [
        ("PUT", "/wiki/api/v2/pages/0100", {"id": "0100"}, "", None),  # not a content id
        ("GET", "/wiki/api/v2/pages/abc", None, "", None),
        ("PUT", "/wiki/api/v2/pages/100", {"id": "101", "title": "T"}, "", None),  # another page in the body
        ("PUT", "/wiki/api/v2/pages/100", {"id": "100", "parentId": "300"}, "", None),  # a move
        ("PUT", "/wiki/api/v2/blogposts/200", {"id": "200", "ownerId": "acc-bob"}, "", None),  # a hand-over
        ("PUT", "/wiki/api/v2/pages/100", b'{"id": "100", "id": "101"}', "", None),
        ("PUT", "/wiki/api/v2/pages/100", b'{"id": "100"}', "", {"content-type": "text/plain"}),
        ("PUT", "/wiki/api/v2/pages/100/title", b"[]", "", None),
        ("DELETE", "/wiki/api/v2/pages/100", None, "purge=true", None),
        ("DELETE", "/wiki/api/v2/blogposts/200", None, "draft=true&purge=1", None),
        # v1 creates: the space by key alone, at the top of the space, a page or blog post.
        ("POST", "/wiki/rest/api/content", {"type": "page", "space": {"key": "DEV", "id": 98308}}, "", None),
        ("POST", "/wiki/rest/api/content", {"type": "page", "space": {"id": 98307}}, "", None),
        ("POST", "/wiki/rest/api/content", {"type": "page", "space": {"key": "DEV"}, "ancestors": [{"id": "100"}]}, "", None),
        ("POST", "/wiki/rest/api/content", {"type": "comment", "space": {"key": "DEV"}, "container": {"id": "100"}}, "", None),
        ("POST", "/wiki/rest/api/content", {"type": "page", "space": {"key": "DEV"}, "restrictions": {}}, "", None),
        ("POST", "/wiki/rest/api/content", {"type": "page", "space": {"key": "bad key"}}, "", None),
        ("POST", "/wiki/rest/api/content", b'{"type": "page", "space": {"key": "DEV"}, "space": {"key": "OPS"}}', "", None),
        ("POST", "/wiki/rest/api/content", b"type=page&space.key=DEV", "", {"content-type": "application/x-www-form-urlencoded"}),
        ("PUT", "/wiki/rest/api/space/bad key", {}, "", None),
    ],
)
def test_requests_that_cannot_be_read_exactly_are_unresolved(method, path, body, query, headers):
    o = ROUTES.match(req(method, path, body, query, headers))
    assert o is not None and o.unresolved, o


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/wiki/api/v2/pages"),  # the space by numeric id
        ("POST", "/wiki/api/v2/footer-comments"),
        ("PUT", "/wiki/api/v2/pages/100/restriction-state"),
        ("POST", "/wiki/api/v2/spaces"),
        ("DELETE", "/wiki/api/v2/attachments/9"),
        ("PUT", "/wiki/rest/api/content/100"),  # v1 by id: page or blog post?
        ("POST", "/wiki/rest/api/content/100/label"),
        ("POST", "/wiki/rest/api/content/100/copy"),
        ("DELETE", "/wiki/rest/api/content/100/pageTree"),
        ("PUT", "/wiki/rest/api/content/100/move/append/300"),
        ("POST", "/wiki/rest/api/space"),
    ],
)
def test_unrouted_writes_have_no_route(method, path):
    assert ROUTES.match(req(method, path, {})) is None
    assert ROUTES.is_unrouted(method, path)


def test_reads_are_untrusted_and_none_sends_out():
    assert ROUTES.match(req("GET", "/wiki/api/v2/pages/100")).untrusted_output  # type: ignore[union-attr]
    assert not any(r.sends_out for r in ROUTES.routes)

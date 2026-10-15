"""The github route table against the REST API description, and its shape."""

from __future__ import annotations

import json

import pytest

from tests.harness.routes import check_routes_against_spec
from toolpass.core.routes import Request
from toolpass.integrations.github.actions import ACTIONS
from toolpass.integrations.github.routes import ROUTES


def req(method: str, path: str, body: dict | None = None) -> Request:
    return Request("https", "api.github.com", 443, method, path, "", {"content-type": "application/json"}, json.dumps(body).encode() if body else None)


def test_routes_match_the_api_description():
    problems = check_routes_against_spec(ROUTES, "github", host="api.github.com")
    assert problems == []


def test_every_route_names_a_known_action():
    for r in ROUTES.routes:
        assert r.action is None or r.action in ACTIONS, r


@pytest.mark.parametrize(
    ("method", "path", "body", "action", "resource", "effect"),
    [
        ("GET", "/repos/acme/x/contents/README.md", None, "repo.read", "repo:acme/x", "read"),
        ("POST", "/repos/acme/x/pulls", None, "pr.create", "repo:acme/x", "write"),
        ("PUT", "/repos/acme/x/pulls/3/merge", None, "pr.merge", "repo:acme/x", "write"),
        ("POST", "/repos/acme/x/issues", None, "issue.create", "repo:acme/x", "write"),
        ("PATCH", "/repos/acme/x/issues/3", None, "repo.triage", "repo:acme/x", "write"),
        ("PUT", "/repos/acme/x/contents/a/b.txt", {"branch": "feat/1"}, "repo.push", "repo:acme/x@feat/1", "write"),
        ("PUT", "/repos/acme/x/contents/a/b.txt", {}, "repo.push", "repo:acme/x", "write"),
        ("POST", "/repos/acme/x/git/refs", {"ref": "refs/heads/new"}, "repo.push", "repo:acme/x@new", "write"),
        ("POST", "/repos/acme/x/git/refs", {"ref": "refs/tags/v1"}, "repo.push", "repo:acme/x", "write"),
        ("DELETE", "/repos/acme/x/git/refs/heads/feature/x", None, "repo.push", "repo:acme/x@feature/x", "destructive"),
        ("POST", "/repos/acme/x/merges", {"base": "main", "head": "f"}, "repo.push", "repo:acme/x@main", "write"),
        ("PUT", "/repos/acme/x/branches/main/protection", None, "repo.admin", "repo:acme/x@main", "write"),
        ("DELETE", "/repos/acme/x", None, "repo.admin", "repo:acme/x", "destructive"),
        ("PATCH", "/repos/acme/x", None, "repo.admin", "repo:acme/x", "write"),
        ("PUT", "/repos/acme/x/actions/secrets/TOKEN", None, "repo.admin", "repo:acme/x", "write"),
        ("POST", "/repos/acme/x/issues/3/comments", None, "repo.read", "repo:acme/x", "write"),
        ("POST", "/orgs/acme/repos", None, "org.repo.create", "org:acme", "write"),
        ("GET", "/orgs/acme/teams/dev/members", None, "team.member", "team:acme/dev", "read"),
        ("PUT", "/orgs/acme/teams/dev/memberships/bob", None, "team.maintainer", "team:acme/dev", "write"),
        ("DELETE", "/orgs/acme/members/bob", None, "org.admin", "org:acme", "destructive"),
        ("GET", "/user", None, None, "", "read"),
        ("GET", "/search/code", None, None, "", "read"),
        ("POST", "/graphql", {"query": "{ viewer { login } }"}, None, "", "read"),
    ],
)
def test_routing(method, path, body, action, resource, effect):
    o = ROUTES.match(req(method, path, body))
    assert o is not None
    assert (o.action, o.resource, o.effect) == (action, resource, effect)
    assert o.unresolved is None


def test_comments_send_out_and_reads_are_untrusted():
    assert ROUTES.match(req("POST", "/repos/acme/x/issues/3/comments")).sends_out
    assert ROUTES.match(req("GET", "/repos/acme/x/issues/3")).untrusted_output
    assert not ROUTES.match(req("GET", "/rate_limit")).untrusted_output


def test_graphql_mutations_are_not_routed():
    o = ROUTES.match(req("POST", "/graphql", {"query": "mutation { createIssue(input: {}) { issue { id } } }"}))
    assert o is not None and o.unresolved is not None and "mutation" in o.unresolved


def test_unknown_writes_have_no_route():
    assert ROUTES.match(req("POST", "/repos/acme/x/some-new-thing")) is None
    assert ROUTES.match(req("POST", "/nowhere")) is None


def test_enterprise_server_prefix():
    s = type("S", (), {"get": lambda self, k: "https://ghe.example.com/base"})()
    assert ROUTES.endpoints(s) == [("ghe.example.com", "/base/api/v3"), ("ghe.example.com", "/base/api")]
    o = ROUTES.match(req("POST", "/base/api/v3/repos/acme/x/pulls"), prefix="/base/api/v3")
    assert o is not None and o.action == "pr.create"
    s = type("S", (), {"get": lambda self, k: ""})()
    assert ROUTES.endpoints(s) == [("api.github.com", ""), ("uploads.github.com", "")]

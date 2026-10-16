"""The gitlab route table against GitLab's OpenAPI description, and its shape."""

from __future__ import annotations

import json
import urllib.parse

import pytest

from tests.harness.routes import check_routes_against_spec
from toolpass.core.catalog import parse_resource
from toolpass.core.integration import Settings, find_action
from toolpass.core.routes import Request
from toolpass.integrations.gitlab import INTEGRATION
from toolpass.integrations.gitlab.actions import ACTIONS, parse_target
from toolpass.integrations.gitlab.routes import ROUTES

P = "/api/v4/projects/acme%2Fwebapp"


def req(method: str, path: str, body: dict | str | None = None, query: str = "", content_type: str = "application/json") -> Request:
    raw = body.encode() if isinstance(body, str) else json.dumps(body).encode() if body is not None else None
    return Request("https", "gitlab.com", 443, method, path, query, {"content-type": content_type}, raw)


def test_routes_match_the_api_description():
    assert check_routes_against_spec(ROUTES, "gitlab", host="gitlab.com") == []


def test_every_route_names_a_known_action():
    for r in ROUTES.routes:
        assert r.action is None or find_action(INTEGRATION, r.action) is not None, r


def test_the_integration_exposes_the_table():
    assert INTEGRATION.routes() is ROUTES


@pytest.mark.parametrize(
    ("method", "path", "body", "action", "resource", "effect"),
    [
        ("GET", P, None, "project.read", "project:acme/webapp", "read"),
        ("GET", "/api/v4/projects/42/repository/tree", None, "project.read", "project:42", "read"),
        ("GET", P + "/variables", None, "variable.manage", "project:acme/webapp", "read"),
        ("GET", P + "/hooks/3", None, "project.admin", "project:acme/webapp", "read"),
        ("GET", P + "/triggers", None, "project.admin", "project:acme/webapp", "read"),
        ("GET", "/api/v4/groups/acme%2Fplatform", None, "group.member", "group:acme/platform", "read"),
        ("GET", "/api/v4/groups/acme/variables", None, "group.admin", "group:acme", "read"),
        ("POST", P + "/issues", {"title": "x"}, "issue.create", "project:acme/webapp", "write"),
        ("PUT", P + "/issues/7", {"state_event": "close"}, "issue.edit", "project:acme/webapp", "write"),
        ("POST", P + "/issues/7/notes", {"body": "hi"}, "project.read", "project:acme/webapp", "write"),
        ("POST", P + "/merge_requests", {"source_branch": "f", "target_branch": "main"}, "mr.create", "project:acme/webapp", "write"),
        ("POST", P + "/merge_requests/3/approve", None, "mr.approve", "project:acme/webapp", "write"),
        ("PUT", P + "/merge_requests/3/merge", {"squash": True}, "mr.merge", "project:acme/webapp", "write"),
        ("POST", P + "/repository/files/src%2Fapp.py", {"branch": "feature/x", "content": "y"}, "repo.push", "project:acme/webapp@feature/x", "write"),
        ("DELETE", P + "/repository/files/a.txt", None, "repo.push", "project:acme/webapp@main", "destructive"),
        ("POST", P + "/repository/commits", {"branch": "main", "actions": []}, "repo.push", "project:acme/webapp@main", "write"),
        ("POST", P + "/repository/branches", {"branch": "feat", "ref": "main"}, "repo.push", "project:acme/webapp@feat", "write"),
        ("DELETE", P + "/repository/branches/release%2F1.0", None, "repo.push", "project:acme/webapp@release/1.0", "destructive"),
        ("POST", P + "/repository/commits/abc/cherry_pick", {"branch": "stable"}, "repo.push", "project:acme/webapp@stable", "write"),
        ("POST", P + "/repository/changelog", {"version": "1.0"}, "repo.push", "project:acme/webapp", "write"),
        ("PUT", P + "/repository/branches/main/protect", None, "branch.protect", "project:acme/webapp", "write"),
        ("POST", P + "/protected_branches", {"name": "main"}, "branch.protect", "project:acme/webapp", "write"),
        ("POST", P + "/members", {"user_id": 5, "access_level": 30}, "member.manage", "project:acme/webapp", "write"),
        ("POST", P + "/pipeline", {"ref": "main"}, "pipeline.run", "project:acme/webapp", "write"),
        ("POST", P + "/jobs/9/retry", None, "pipeline.run", "project:acme/webapp", "write"),
        ("POST", P + "/variables", {"key": "K", "value": "v"}, "variable.manage", "project:acme/webapp", "write"),
        ("POST", P + "/runners", {"runner_id": 1}, "runner.manage", "project:acme/webapp", "write"),
        ("PUT", P, {"description": "x"}, "project.admin", "project:acme/webapp", "write"),
        ("DELETE", P, None, "project.delete", "project:acme/webapp", "destructive"),
        ("POST", P + "/hooks", {"url": "https://x"}, "project.admin", "project:acme/webapp", "write"),
        ("POST", P + "/wikis", {"title": "t", "content": "c"}, "project.admin", "project:acme/webapp", "write"),
        ("POST", P + "/fork", {}, "project.read", "project:acme/webapp", "write"),
        ("POST", "/api/v4/projects", {"name": "new", "namespace_id": 12}, "group.project.create", "group:12", "write"),
        ("POST", "/api/v4/projects", {"name": "mine"}, None, "", "write"),
        ("POST", "/api/v4/groups", {"name": "sub", "path": "sub", "parent_id": 4}, "group.admin", "group:4", "write"),
        ("PUT", "/api/v4/groups/acme", {"description": "x"}, "group.admin", "group:acme", "write"),
        ("POST", "/api/v4/groups/acme/members", {"user_id": 5, "access_level": 50}, "group.admin", "group:acme", "write"),
        ("POST", P + "/ci/lint", {"content": "x"}, "project.read", "project:acme/webapp", "read"),
        ("GET", "/api/v4/user", None, None, "", "read"),
    ],
)
def test_routing(method, path, body, action, resource, effect):
    query = "branch=main" if path.endswith("a.txt") else ""
    o = ROUTES.match(req(method, path, body, query))
    assert o is not None and o.unresolved is None, o
    assert (o.action, o.resource, o.effect) == (action, resource, effect)
    if action is not None:
        # The resource is in the form the integration's own check parses.
        t = parse_target(ACTIONS[action], parse_resource(resource))
        assert t.id and t.scope == ACTIONS[action].scope


def test_a_form_body_and_the_query_are_read_too():
    o = ROUTES.match(req("PUT", P + "/repository/files/a.txt", "branch=dev&content=x", content_type="application/x-www-form-urlencoded"))
    assert o is not None and o.resource == "project:acme/webapp@dev"
    o = ROUTES.match(req("POST", P + "/repository/branches", None, query="branch=hotfix&ref=main"))
    assert o is not None and o.resource == "project:acme/webapp@hotfix"


@pytest.mark.parametrize(
    ("method", "path", "body", "query", "content_type"),
    [
        ("POST", P + "/repository/files/a.txt", {"content": "x"}, "", "application/json"),  # no branch
        ("POST", P + "/repository/files/a.txt", {"branch": "main"}, "branch=other", "application/json"),  # two branches
        ("POST", P + "/repository/files/a.txt", "branch=main&branch=other", "", "application/x-www-form-urlencoded"),
        ("POST", P + "/repository/files/a.txt", "branch[]=main", "", "application/x-www-form-urlencoded"),
        ("POST", P + "/repository/files/a.txt", "branch=main;branch=other", "", "application/x-www-form-urlencoded"),
        ("POST", P + "/repository/files/a.txt", '{"branch": "main", "branch": "x"}', "", "application/json"),
        ("POST", P + "/repository/files/a.txt", '{"branch": "main"}', "", "text/plain"),  # a type GitLab may read differently
        ("POST", P + "/repository/files/a.txt", {"branch": ["main"]}, "", "application/json"),
        ("POST", P + "/repository/files/a.txt", {"branch": "a@b"}, "", "application/json"),
        ("POST", P + "/repository/files/a.txt", {"branch": "bad..name"}, "", "application/json"),
        ("POST", P + "/repository/commits", {"branch": "main", "force": True}, "", "application/json"),  # force-push
        ("POST", P + "/repository/commits", {"branch": "main", "start_project": 99}, "", "application/json"),  # second project
        ("DELETE", P + "/repository/branches/bad~name", None, "", "application/json"),
        ("POST", P + "/merge_requests", {"source_branch": "f", "target_branch": "main", "target_project_id": 99}, "", "application/json"),
        ("PUT", P + "/merge_requests/3/merge", {"should_remove_source_branch": True}, "", "application/json"),
        ("POST", P + "/issues/7/links", {"target_project_id": "other/proj", "target_issue_iid": 1}, "", "application/json"),
        ("POST", P + "/members", {"user_id": 5, "access_level": 50}, "", "application/json"),  # Owner
        ("POST", P + "/members", {"user_id": 5, "access_level": 30, "member_role_id": 3}, "", "application/json"),
        ("POST", P + "/invitations", {"email": "a@b.c", "access_level": 60}, "", "application/json"),
        ("POST", P + "/fork", {"namespace_path": "elsewhere"}, "", "application/json"),
        ("POST", P + "/releases", {"tag_name": "v1", "ref": "main"}, "", "application/json"),  # creates a tag
        ("POST", "/api/v4/projects", {"name": "x", "template_project_id": 3}, "", "application/json"),
        ("POST", "/api/v4/projects", {"name": "x", "namespace_id": "acme"}, "", "application/json"),
        ("POST", "/api/v4/groups", {"name": "top", "path": "top"}, "", "application/json"),
        ("POST", P + "/access_tokens/self/rotate", None, "", "application/json"),
        ("PUT", "/api/v4/projects/acme%2F..%2Fother", {}, "", "application/json"),  # not a project path
        ("PUT", "/api/v4/projects/acme%252Fwebapp", {}, "", "application/json"),  # double-encoded
        ("PUT", P, {"id": 99}, "", "application/json"),  # a parameter naming another project
    ],
)
def test_requests_toolpass_cannot_check_exactly_are_unresolved(method, path, body, query, content_type):
    o = ROUTES.match(req(method, path, body, query, content_type))
    assert o is not None and o.unresolved, o
    assert o.resource is None


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("DELETE", P + "/issues/7"),  # Owner only
        ("POST", P + "/issues/7/move"),  # into another project
        ("PUT", P + "/members/5"),  # the member may be an Owner
        ("DELETE", P + "/members/5"),
        ("POST", P + "/repository/tags"),
        ("PUT", P + "/transfer"),
        ("POST", P + "/share"),
        ("PUT", P + "/merge_requests/3/rebase"),
        ("POST", "/api/v4/admin/ci/variables"),  # outside projects and groups
        ("POST", "/api/v4/graphql"),
        ("POST", "/acme/webapp.git/git-receive-pack"),  # git over HTTPS
    ],
)
def test_writes_with_no_route_are_refused_at_the_wire(method, path):
    assert ROUTES.match(req(method, path, {})) is None


def test_unrouted_writes_are_listed_with_a_reason():
    assert ROUTES.is_unrouted("DELETE", "/api/v4/projects/1/issues/2")
    assert ROUTES.is_unrouted("PUT", "/api/v4/projects/1/packages/npm/x")


def test_comments_and_new_issues_send_out_and_reads_are_untrusted():
    assert ROUTES.match(req("POST", P + "/issues", {"title": "x"})).sends_out  # type: ignore[union-attr]
    assert ROUTES.match(req("POST", P + "/merge_requests/1/notes", {"body": "x"})).sends_out  # type: ignore[union-attr]
    assert ROUTES.match(req("GET", P + "/issues")).untrusted_output  # type: ignore[union-attr]


def test_the_whole_instance_is_where_the_api_lives():
    assert ROUTES.endpoints(Settings("gl", "gitlab", {})) == [("gitlab.com", "")]
    assert ROUTES.endpoints(Settings("gl", "gitlab", {"url": "https://git.acme.com/gitlab/"})) == [("git.acme.com", "/gitlab")]
    o = ROUTES.match(req("PUT", "/gitlab" + P, {}), "/gitlab")
    assert o is not None and (o.action, o.resource) == ("project.admin", "project:acme/webapp")


def test_an_encoded_slash_stays_inside_the_project_segment():
    path = "/api/v4/projects/" + urllib.parse.quote("acme/sub/webapp", safe="") + "/issues"
    o = ROUTES.match(req("POST", path, {"title": "x"}))
    assert o is not None and o.resource == "project:acme/sub/webapp"
    # An unencoded path is a different (and non-existent) project to GitLab, and to toolpass.
    o = ROUTES.match(req("POST", "/api/v4/projects/acme/webapp/issues", {"title": "x"}))
    assert o is None or o.resource != "project:acme/webapp"


def test_a_pipeline_schedule_is_read_as_its_variables_are():
    # Its page lists the schedule's variables with their values.
    sched = ROUTES.match(req("GET", "/api/v4/projects/42/pipeline_schedules/7"))
    variables = ROUTES.match(req("GET", "/api/v4/projects/42/variables"))
    assert sched is not None and variables is not None and sched.unresolved is None
    assert sched.action == variables.action

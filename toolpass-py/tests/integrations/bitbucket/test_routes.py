"""The bitbucket route table: Cloud against Bitbucket's API description, the
Cloud workspace held to the connection's, and Data Center's REST paths."""

from __future__ import annotations

import json

import pytest

from tests.harness.routes import check_routes_against_spec
from toolpass.core.catalog import parse_resource
from toolpass.core.integration import Settings, find_action
from toolpass.core.routes import Request
from toolpass.integrations.bitbucket import INTEGRATION
from toolpass.integrations.bitbucket.actions import parse_target
from toolpass.integrations.bitbucket.routes import ROUTES

CLOUD = Settings("bb", "bitbucket", {"edition": "cloud", "workspace": "acme"})
DC = Settings("bbdc", "bitbucket", {"edition": "datacenter", "url": "https://git.acme.com/bitbucket"})
R = "/2.0/repositories/acme/api"
D = "/bitbucket/rest/api/1.0/projects/APP/repos/api"


def req(method: str, path: str, body: dict | str | bytes | None = None, content_type: str = "application/json", host: str = "api.bitbucket.org") -> Request:
    raw = body if isinstance(body, bytes) else body.encode() if isinstance(body, str) else json.dumps(body).encode() if body is not None else None
    return Request("https", host, 443, method, path, "", {"content-type": content_type}, raw)


def cloud(method: str, path: str, body: dict | str | bytes | None = None, content_type: str = "application/json"):
    """Match as the wire hook does: the connection's longest endpoint prefix for the path."""
    r = req(method, path, body, content_type)
    prefixes = [p for h, p in ROUTES.endpoints(CLOUD) if h == "api.bitbucket.org" and (r.path == p or r.path.startswith(p + "/") or p == "")]
    return ROUTES.match(r, max(prefixes, key=len))


def dc(method: str, path: str, body: dict | str | bytes | None = None, content_type: str = "application/json"):
    return ROUTES.match(req(method, path, body, content_type, host="git.acme.com"), "/bitbucket")


def multipart(fields: list[tuple[str, str, str | None]]) -> tuple[bytes, str]:
    out = b""
    for name, value, filename in fields:
        fn = f'; filename="{filename}"' if filename else ""
        out += f'--XyZ\r\nContent-Disposition: form-data; name="{name}"{fn}\r\n\r\n{value}\r\n'.encode()
    return out + b"--XyZ--\r\n", "multipart/form-data; boundary=XyZ"


def test_routes_match_the_api_description():
    assert check_routes_against_spec(ROUTES, "bitbucket-cloud", host="api.bitbucket.org") == []


def test_every_route_names_a_known_action():
    for r in ROUTES.routes:
        assert r.action is None or find_action(INTEGRATION, r.action) is not None, r


def test_the_integration_exposes_the_table():
    assert INTEGRATION.routes() is ROUTES


def test_endpoints():
    assert ROUTES.endpoints(CLOUD) == [
        ("api.bitbucket.org", ""),
        ("api.bitbucket.org", "/2.0"),
        ("api.bitbucket.org", "/2.0/repositories/acme"),
        ("api.bitbucket.org", "/2.0/workspaces/acme"),
    ]
    assert ROUTES.endpoints(DC) == [("git.acme.com", "/bitbucket")]


def _parses(action: str, resource: str, data_center: bool) -> None:
    """The resource is in the form the integration's own check parses."""
    parse_target(action, parse_resource(resource), data_center)


@pytest.mark.parametrize(
    ("method", "path", "body", "action", "resource", "effect"),
    [
        ("GET", R, None, "repo.read", "repo:api", "read"),
        ("GET", R + "/pullrequests/4/diff", None, "repo.read", "repo:api", "read"),
        ("GET", R + "/pipelines_config/variables", None, "repo.admin", "repo:api", "read"),
        ("GET", R + "/hooks", None, "repo.admin", "repo:api", "read"),
        ("GET", "/2.0/repositories/acme", None, "workspace.member", "workspace", "read"),
        ("GET", "/2.0/workspaces/acme/projects/APP", None, "project.read", "project:APP", "read"),
        ("GET", "/2.0/workspaces/acme/permissions", None, "workspace.admin", "workspace", "read"),
        ("POST", R, {"scm": "git", "project": {"key": "APP"}}, "repo.create", "project:APP", "write"),
        ("PUT", R, {"description": "x"}, "repo.admin", "repo:api", "write"),
        ("DELETE", R, None, "repo.admin", "repo:api", "destructive"),
        ("POST", R + "/refs/branches", {"name": "feature/x", "target": {"hash": "abc"}}, "repo.push", "repo:api@feature/x", "write"),
        ("DELETE", R + "/refs/branches/feature/x", None, "repo.push", "repo:api@feature/x", "destructive"),
        ("POST", R + "/refs/tags", {"name": "v1", "target": {"hash": "abc"}}, "repo.push", "repo:api", "write"),
        ("POST", R + "/pullrequests", {"title": "t", "source": {"branch": {"name": "f"}}}, "repo.read", "repo:api", "write"),
        ("PUT", R + "/pullrequests/4", {"title": "t"}, "repo.push", "repo:api", "write"),
        ("POST", R + "/pullrequests/4/approve", None, "repo.read", "repo:api", "write"),
        ("POST", R + "/pullrequests/4/comments", {"content": {"raw": "hi"}}, "repo.read", "repo:api", "write"),
        ("DELETE", R + "/pullrequests/4/comments/9", None, "repo.admin", "repo:api", "destructive"),
        ("POST", R + "/pullrequests/4/decline", None, "repo.push", "repo:api", "write"),
        ("POST", R + "/commit/abc/statuses/build", {"state": "SUCCESSFUL"}, "repo.push", "repo:api", "write"),
        ("POST", R + "/pipelines", {"target": {"ref_name": "main"}}, "repo.push", "repo:api", "write"),
        ("POST", R + "/branch-restrictions", {"kind": "push"}, "repo.admin", "repo:api", "write"),
        ("PUT", R + "/permissions-config/users/x", {"permission": "write"}, "repo.admin", "repo:api", "write"),
        ("POST", R + "/commits", None, "repo.read", "repo:api", "read"),
        ("POST", "/2.0/workspaces/acme/codesearch", {"search_query": "x"}, "workspace.member", "workspace", "read"),
        ("POST", "/2.0/workspaces/acme/projects", {"key": "NEW"}, "workspace.admin", "workspace", "write"),
        ("PUT", "/2.0/workspaces/acme/projects/APP", {"name": "x"}, "project.admin", "project:APP", "write"),
        ("POST", "/2.0/workspaces/acme/hooks", {"url": "https://x"}, "workspace.admin", "workspace", "write"),
        ("GET", "/2.0/user", None, None, "", "read"),
    ],
)
def test_cloud_routing(method, path, body, action, resource, effect):
    o = cloud(method, path, body)
    assert o is not None and o.unresolved is None, o
    assert (o.action, o.resource, o.effect) == (action, resource, effect)
    if action is not None:
        _parses(action, resource, False)


def test_a_file_commit_with_the_branch_in_the_query_is_unresolved():
    r = Request("https", "api.bitbucket.org", 443, "POST", R + "/src", "branch=dev", {"content-type": "application/x-www-form-urlencoded"}, b"branch=main")
    o = ROUTES.match(r, "/2.0/repositories/acme")
    assert o is not None and o.unresolved


def test_a_file_commit_names_its_branch_in_the_form():
    o = cloud("POST", R + "/src", "branch=main&message=m&%2FREADME.md=hello", "application/x-www-form-urlencoded")
    assert o is not None and (o.action, o.resource, o.sends_out) == ("repo.push", "repo:api@main", True)
    body, ct = multipart([("message", "m", None), ("branch", "release/2", None), ("/a.txt", "data", "a.txt")])
    o = cloud("POST", R + "/src", body, ct)
    assert o is not None and o.resource == "repo:api@release/2", o
    _parses("repo.push", "repo:api@release/2", False)


@pytest.mark.parametrize(
    ("body", "content_type"),
    [
        ("message=m&%2FREADME.md=hello", "application/x-www-form-urlencoded"),  # the main branch, unnamed
        ("branch=main&branch=dev", "application/x-www-form-urlencoded"),
        ("branch=main;branch=dev", "application/x-www-form-urlencoded"),
        ("Branch=main", "application/x-www-form-urlencoded"),
        ('{"branch": "main"}', "application/json"),  # /src takes a form
        multipart([("branch", "main", None), ("branch", "dev", None)]),
        multipart([("branch", "main", "branch.txt")]),  # the field as a file part
        multipart([("BRANCH", "main", None)]),
        (
            b'--XyZ\r\nContent-Disposition: form-data; name="x"\r\nContent-Disposition: form-data; name="branch"\r\n\r\nmain\r\n--XyZ--\r\n',
            "multipart/form-data; boundary=XyZ",
        ),
        (b'--XyZ\r\nContent-Disposition: form-data; name="branch"\r\n\r\nmain\r\n', "multipart/form-data; boundary=XyZ"),  # never closed
    ],
)
def test_a_file_commit_without_one_plain_branch_is_unresolved(body, content_type):
    if isinstance(body, tuple):
        body, content_type = body
    o = cloud("POST", R + "/src", body, content_type)
    assert o is not None and o.unresolved and o.resource is None, o


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("PUT", "/2.0/repositories/other/api", {"description": "x"}),  # another workspace
        ("GET", "/2.0/repositories/other/api", None),
        ("POST", "/2.0/repositories/ACME/api/refs/branches", {"name": "x"}),  # not the connection's spelling
        ("POST", "/2.0/workspaces/other/projects", {"key": "NEW"}),
        ("POST", "/2.0/repositories/%7B6a9e1d9c-0000-0000-0000-000000000000%7D/api/refs/tags", {"name": "v1"}),  # a workspace UUID
        ("POST", R + "/pullrequests/4/merge", {"merge_strategy": "squash"}),  # the destination branch is not named
        ("POST", R + "/refs/branches", {"target": {"hash": "abc"}}),
        ("POST", R + "/refs/branches", {"name": "x@y"}),
        ("POST", R + "/refs/branches", {"name": "bad..branch"}),
        ("POST", R + "/refs/branches", '{"name": "a", "name": "b"}'),
        ("POST", R, {"scm": "git"}),  # no project
        ("POST", R, {"project": {"key": "APP", "uuid": "{x}"}}),
        ("PUT", R, {"project": {"key": "OTHER"}}),  # moves the repository
        ("POST", R + "/pullrequests", {"source": {"branch": {"name": "f"}, "repository": {"full_name": "evil/fork"}}}),
        ("POST", R + "/pullrequests", {"destination": {"repository": {"full_name": "acme/other"}}}),
        ("DELETE", "/2.0/repositories/acme/bad%20slug", None),
        ("DELETE", R + "/refs/branches/refs/heads/main", None),  # a branch literally named refs/heads/main, not main
    ],
)
def test_cloud_requests_toolpass_cannot_check_exactly_are_unresolved(method, path, body):
    o = cloud(method, path, body)
    assert o is not None and o.unresolved and o.resource is None, o


def test_cloud_writes_with_no_route_are_refused_at_the_wire():
    assert cloud("POST", R + "/forks", {"workspace": {"slug": "acme"}}) is None
    assert ROUTES.is_unrouted("POST", "/repositories/acme/api/forks")
    assert cloud("PUT", R + "/properties/app/prop", {}) is None
    assert cloud("POST", "/2.0/teams/acme/pipelines_config/variables", {}) is None
    assert cloud("POST", "/internal/anything", {}) is None


@pytest.mark.parametrize(
    ("method", "path", "body", "action", "resource", "effect"),
    [
        ("GET", D, None, "repo.read", "repo:APP/api", "read"),
        ("GET", "/bitbucket/rest/api/latest/projects/APP/repos/api/pull-requests", None, "repo.read", "repo:APP/api", "read"),
        ("GET", "/bitbucket/rest/api/1.0/projects/APP", None, "project.read", "project:APP", "read"),
        ("GET", D + "/permissions/users", None, "repo.admin", "repo:APP/api", "read"),
        ("POST", "/bitbucket/rest/api/1.0/projects/APP/repos", {"name": "new", "scmId": "git"}, "repo.create", "project:APP", "write"),
        ("PUT", D, {"name": "renamed"}, "repo.admin", "repo:APP/api", "write"),
        ("DELETE", D, None, "repo.admin", "repo:APP/api", "destructive"),
        (
            "POST",
            D + "/pull-requests",
            {"title": "t", "fromRef": {"id": "refs/heads/f"}, "toRef": {"id": "refs/heads/main"}},
            "repo.read",
            "repo:APP/api",
            "write",
        ),
        ("POST", D + "/pull-requests/3/approve", None, "repo.read", "repo:APP/api", "write"),
        ("POST", D + "/pull-requests/3/comments", {"text": "hi"}, "repo.read", "repo:APP/api", "write"),
        ("PUT", D + "/settings/hooks/x/enabled", None, "repo.admin", "repo:APP/api", "write"),
        ("POST", D + "/webhooks", {"url": "https://x"}, "repo.admin", "repo:APP/api", "write"),
        (
            "POST",
            "/bitbucket/rest/branch-utils/1.0/projects/APP/repos/api/branches",
            {"name": "feat", "startPoint": "main"},
            "repo.push",
            "repo:APP/api@feat",
            "write",
        ),
        (
            "DELETE",
            "/bitbucket/rest/branch-utils/latest/projects/APP/repos/api/branches",
            {"name": "refs/heads/feat"},
            "repo.push",
            "repo:APP/api@feat",
            "destructive",
        ),
        ("POST", "/bitbucket/rest/api/1.0/projects", {"key": "NEW"}, "workspace.admin", "workspace", "write"),
    ],
)
def test_data_center_routing(method, path, body, action, resource, effect):
    o = dc(method, path, body)
    assert o is not None and o.unresolved is None, o
    assert (o.action, o.resource, o.effect) == (action, resource, effect)
    _parses(action, resource, True)


def test_a_data_center_file_edit_names_its_branch():
    body, ct = multipart([("branch", "main", None), ("message", "m", None), ("content", "x", "a.txt")])
    o = dc("PUT", D + "/browse/src/a.txt", body, ct)
    assert o is not None and (o.action, o.resource) == ("repo.push", "repo:APP/api@main"), o
    body, ct = multipart([("message", "m", None), ("content", "x", "a.txt")])
    o = dc("PUT", D + "/browse/src/a.txt", body, ct)
    assert o is not None and o.unresolved


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("POST", D, {"project": {"key": "OTHER"}}),  # a fork
        ("PUT", D, {"project": {"key": "OTHER"}}),  # a move
        ("POST", D + "/pull-requests/3/merge", None),
        ("POST", D + "/pull-requests", {"fromRef": {"id": "refs/heads/f", "repository": {"slug": "fork", "project": {"key": "~EVE"}}}}),
        ("POST", "/bitbucket/rest/api/1.0/projects/APP/repos", {"name": "x", "project": {"key": "OTHER"}}),
        ("POST", "/bitbucket/rest/branch-utils/1.0/projects/APP/repos/api/branches", {"name": "refs/tags/v1"}),
        ("GET", "/bitbucket/rest/api/1.0/projects/APP/repos/bad%2Fslug", None),
        ("POST", "/bitbucket/repositories/acme/api/refs/branches", {"name": "x"}),  # a Cloud path on Data Center
    ],
)
def test_data_center_requests_toolpass_cannot_check_exactly_are_unresolved(method, path, body):
    o = dc(method, path, body)
    assert o is not None and o.unresolved and o.resource is None, o


def test_data_center_writes_with_no_route_are_refused_at_the_wire():
    assert dc("POST", "/bitbucket/rest/git/1.0/projects/APP/repos/api/tags", {"name": "v1"}) is None
    assert dc("POST", "/bitbucket/rest/build-status/1.0/commits/abc", {}) is None
    assert dc("POST", "/bitbucket/scm/app/api.git/git-receive-pack", b"0000", "application/x-git-receive-pack-request") is None


def test_comments_and_pull_requests_send_out_and_reads_are_untrusted():
    assert cloud("POST", R + "/pullrequests/4/comments", {"content": {"raw": "x"}}).sends_out  # type: ignore[union-attr]
    assert dc("POST", D + "/pull-requests", {"title": "t"}).sends_out  # type: ignore[union-attr]
    assert cloud("GET", R + "/src/main/README.md").untrusted_output  # type: ignore[union-attr]

"""Findings of the security review, each kept closed by a test."""

from __future__ import annotations

import json

import pytest

from toolpass.core.routes import Request
from toolpass.integrations.github.routes import ROUTES as GITHUB
from toolpass.integrations.slack.routes import ROUTES as SLACK


def gh(method: str, path: str, body: dict | None = None, query: str = "", raw: bytes | None = None, ct: str = "application/json") -> Request:
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    return Request("https", "api.github.com", 443, method, path, query, {"content-type": ct}, data)


@pytest.mark.parametrize(
    "document",
    [
        'mutation { deleteRef(input: {refId: "x"}) { clientMutationId } }',
        "# a comment first\nmutation { x }",
        ",mutation { x }",
        "﻿mutation { x }",
        "query A { viewer { login } } mutation B { x }",
        "query A { viewer { login } } query B { viewer { id } }",
        "{ viewer { login } } { viewer { id } }",
        "subscription { x }",
        "",
        "   ",
        "query { viewer { login }",  # unbalanced
        "viewer { login }",  # not a document
    ],
)
def test_graphql_documents_that_are_not_one_query_are_refused(document):
    o = GITHUB.match(gh("POST", "/graphql", {"query": document, "operationName": "B"}))
    assert o is not None and o.unresolved is not None, document


@pytest.mark.parametrize(
    "document",
    [
        "{ viewer { login } }",
        "query { viewer { login } }",
        "query Me($n: Int!) { viewer { repositories(first: $n) { nodes { name } } } }",
        "# leading comment\nquery { viewer { login } }",
        'query { search(query: "mutation subscription", type: REPOSITORY, first: 1) { repositoryCount } }',
        'query { repository(owner: "acme", name: "x") { description } } fragment F on Repository { name }',
        "query Q { ...F } fragment F on Query { viewer { login } }",
    ],
)
def test_single_queries_are_reads(document):
    o = GITHUB.match(gh("POST", "/graphql", {"query": document}))
    assert o is not None and o.unresolved is None and o.effect == "read" and o.action is None, document


def test_forking_into_the_organization_creates_a_repository_there():
    own = GITHUB.match(gh("POST", "/repos/acme/x/forks", {}))
    assert own is not None and (own.action, own.resource) == ("repo.read", "repo:acme/x")
    org = GITHUB.match(gh("POST", "/repos/acme/x/forks", {"organization": "acme"}))
    assert org is not None and (org.action, org.resource) == ("org.repo.create", "org:acme")


def test_generating_from_a_template_creates_a_repository_in_the_owner():
    o = GITHUB.match(gh("POST", "/repos/acme/template/generate", {"owner": "acme", "name": "new"}))
    assert o is not None and (o.action, o.resource) == ("org.repo.create", "org:acme")
    o = GITHUB.match(gh("POST", "/repos/acme/template/generate", {"name": "new"}))
    assert o is not None and o.unresolved is not None


def test_text_people_read_sends_out():
    for method, path in (
        ("POST", "/repos/acme/x/issues"),
        ("POST", "/repos/acme/x/pulls"),
        ("PUT", "/repos/acme/x/contents/a.txt"),
        ("POST", "/repos/acme/x/releases"),
        ("PATCH", "/repos/acme/x/releases/3"),
        ("POST", "/repos/acme/x/releases/3/assets"),
        ("PATCH", "/repos/acme/x"),
    ):
        o = GITHUB.match(gh(method, path, {}))
        assert o is not None and o.sends_out, (method, path)
    for method in ("conversations.setTopic", "conversations.setPurpose", "conversations.rename"):
        o = SLACK.match(
            Request(
                "https", "slack.com", 443, "POST", f"/api/{method}", "", {"content-type": "application/json"}, json.dumps({"channel": "C0123456789"}).encode()
            )
        )
        assert o is not None and o.sends_out, method
    for method in ("users.profile.set", "reminders.add", "conversations.create"):
        o = SLACK.match(Request("https", "slack.com", 443, "POST", f"/api/{method}", "", {}, b""))
        assert o is not None and o.sends_out, method


def test_a_field_given_twice_is_ambiguous():
    form = "application/x-www-form-urlencoded"
    r = Request("https", "slack.com", 443, "POST", "/api/chat.postMessage", "", {"content-type": form}, b"channel=C0123456789&channel=C9876543210&text=hi")
    assert r.ambiguous() == {"channel"}
    o = SLACK.match(r)
    assert o is not None and o.unresolved is not None and "more than once" in o.unresolved
    # Query and body disagreeing is ambiguous too; agreeing is not.
    r = Request(
        "https",
        "slack.com",
        443,
        "POST",
        "/api/chat.postMessage",
        "channel=C0123456789",
        {"content-type": "application/json"},
        json.dumps({"channel": "C9876543210", "text": "hi"}).encode(),
    )
    assert "channel" in r.ambiguous()
    r = Request(
        "https",
        "slack.com",
        443,
        "POST",
        "/api/chat.postMessage",
        "channel=C0123456789",
        {"content-type": "application/json"},
        json.dumps({"channel": "C0123456789", "text": "hi"}).encode(),
    )
    assert r.ambiguous() == set()
    # The branch of a push, too.
    o = GITHUB.match(gh("PUT", "/repos/acme/x/contents/a.txt", raw=b"branch=main&branch=feature&message=m", ct=form))
    assert o is not None and o.unresolved is not None
    # Multipart duplicates.
    boundary = "b"
    part = 'Content-Disposition: form-data; name="channels"\r\n\r\n{}\r\n'
    body = (f"--{boundary}\r\n" + part.format("C0123456789") + f"--{boundary}\r\n" + part.format("C9876543210") + f"--{boundary}--\r\n").encode()
    r = Request("https", "slack.com", 443, "POST", "/api/files.upload", "", {"content-type": f"multipart/form-data; boundary={boundary}"}, body)
    assert r.ambiguous() == {"channels"}


def test_release_uploads_are_github_too():
    s = type("S", (), {"get": lambda self, k: ""})()
    assert GITHUB.endpoints(s) == [("api.github.com", ""), ("uploads.github.com", "")]
    o = GITHUB.match(Request("https", "uploads.github.com", 443, "POST", "/repos/acme/x/releases/3/assets", "name=a.zip", {}, b"\x00\x01"))
    assert o is not None and o.action == "repo.push" and o.sends_out

"""The linear route table: one GraphQL endpoint, the operation read from the body."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from toolpass.core.integration import Settings
from toolpass.core.routes import Request
from toolpass.integrations.linear.actions import ACTIONS
from toolpass.integrations.linear.routes import MUTATIONS, ROUTES, read_operation


def gql(query: str, variables: dict | None = None) -> Request:
    body = {"query": query, **({"variables": variables} if variables is not None else {})}
    return Request("https", "api.linear.app", 443, "POST", "/graphql", "", {"content-type": "application/json"}, json.dumps(body).encode())


def match(query: str, variables: dict | None = None):
    o = ROUTES.match(gql(query, variables), "/graphql")
    assert o is not None
    return o


def test_the_endpoint():
    assert ROUTES.endpoints(Settings("linear", "linear", {"url": "https://api.linear.app/graphql"})) == [("api.linear.app", "/graphql")]


def test_every_mutation_names_a_known_action():
    for action, _, _ in MUTATIONS.values():
        assert action in ACTIONS


def test_mutations_and_their_arguments_exist_in_linears_schema():
    specs = os.environ.get("TOOLPASS_SPECS_DIR")
    path = Path(specs or "", "linear.spec")
    if not specs or not path.is_file():
        pytest.skip("TOOLPASS_SPECS_DIR not set")
    from tests.harness.graphql import new_graphql

    schema = new_graphql("linear", path.read_bytes())
    mutation = schema.types["Mutation"].fields
    for name, (_, _, where) in MUTATIONS.items():
        assert name in mutation, name
        arg, _, member = where.partition(".")
        assert arg in mutation[name].args, (name, arg)
        if member:
            input_type = mutation[name].args[arg].typ.base()
            assert member in schema.types[input_type].fields, (name, input_type, member)


def test_a_query_is_a_read_without_a_permission_check():
    o = match("query Me { viewer { id name } issues(first: 5) { nodes { id } } }")
    assert (o.action, o.resource, o.effect, o.unresolved, o.untrusted_output) == (None, "", "read", None, True)
    o = match('{ issue(id: "ENG-1") { title } }')
    assert (o.action, o.effect) == (None, "read")


@pytest.mark.parametrize(
    ("query", "variables", "action", "resource"),
    [
        ("mutation IssueCreate($input: IssueCreateInput!) { issueCreate(input: $input) { success issue { id } } }",
         {"input": {"teamId": "5b0f6a2e-1c1e-4c6f-9d3a-2f6c1d0f8e11", "title": "x"}}, "team.member", "team:5b0f6a2e-1c1e-4c6f-9d3a-2f6c1d0f8e11"),
        ('mutation { issueCreate(input: {teamId: "ENG", title: "inline"}) { success } }', None, "team.member", "team:ENG"),
        ('mutation { issueUpdate(id: "ENG-12", input: {title: "y"}) { success } }', None, "issue.edit", "issue:ENG-12"),
        ("mutation D($id: String!) { issueDelete(id: $id) { success } }", {"id": "ENG-12"}, "issue.edit", "issue:ENG-12"),
        ("mutation C($input: CommentCreateInput!) { commentCreate(input: $input) { success } }",
         {"input": {"issueId": "ENG-7", "body": "hi"}}, "issue.edit", "issue:ENG-7"),
        ('mutation { renamed: issueArchive(id: "ENG-3") { success } }', None, "issue.edit", "issue:ENG-3"),
    ],
)  # fmt: skip
def test_known_mutations(query, variables, action, resource):
    o = match(query, variables)
    assert o.unresolved is None, o.unresolved
    assert (o.action, o.resource, o.effect) == (action, resource, "write")


@pytest.mark.parametrize(
    ("query", "variables"),
    [
        ('mutation { teamDelete(id: "ENG") { success } }', None),  # not routed
        ('mutation { a: issueDelete(id: "ENG-1") { success } b: issueDelete(id: "ENG-2") { success } }', None),  # two fields
        ("mutation C($input: IssueCreateInput!) { issueCreate(input: $input) { success } }", {"input": {"title": "no team"}}),
        ('mutation { issueUpdate(id: "../../x", input: {}) { success } }', None),
        ("subscription { issueUpdates { id } }", None),
        ("query A { viewer { id } } query B { viewer { id } }", None),  # two operations
        ('mutation { issueDelete(id: "ENG-1") { ...F } } fragment F on IssuePayload { success }', None),
        ("{ viewer { id ", None),  # not well formed
        ('mutation { issueDelete(id: "unterminated) { success } }', None),
    ],
)
def test_anything_else_is_unresolved(query, variables):
    o = match(query, variables)
    assert o.unresolved, (query, o)


def test_strings_and_comments_cannot_hide_a_mutation():
    o = match('# mutation { issueDelete(id: "ENG-1") }\n{ issues(filter: {title: {eq: "mutation"}}) { nodes { id } } }')
    assert (o.action, o.effect, o.unresolved) == (None, "read", None)


def test_read_operation():
    assert read_operation('mutation M($i: X!) @d { f(a: $i, b: [1, 2], c: {d: "e"}) { x } }') == (
        "mutation",
        [("f", {"a": ("$", "i"), "b": [("literal", "1"), ("literal", "2")], "c": {"d": "e"}})],
    )


def test_an_escaped_triple_quote_does_not_end_a_block_string():
    # The server reads issueUpdate on ENG-1 and then issueDelete on ENG-9; so must toolpass.
    q = 'mutation { issueUpdate(id: "ENG-1", input: {title: """a\\""", description: """ }) { success } issueDelete(id: "ENG-9") { success } } # """ }) { success } }'
    o = match(q)
    assert o is not None and o.unresolved, "two mutations: not one toolpass can check as one"
    assert [f for f, _ in read_operation('query { issue(id: "x") { title(f: """a\\"""b""") } }')[1]] == ["issue"]


def test_a_bad_unicode_escape_is_unreadable():
    o = match('mutation { issueDelete(id: "\\uZZZZ") { success } }')
    assert o is not None and o.unresolved


@pytest.mark.parametrize(
    "query",
    [
        'mutation { issueUpdate(id: "ENG-1", input: {teamId: "OPS"}) { success } }',  # moves it to a team not checked
        'mutation { issueUpdate(id: "ENG-1", input: {parentId: "OPS-2"}) { success } }',
        'mutation { commentCreate(input: {issueId: "ENG-1", projectUpdateId: "x"}) { success } }',
        'mutation { issueRelationCreate(input: {issueId: "ENG-1", relatedIssueId: "OPS-2", type: blocks}) { success } }',
    ],
)
def test_a_mutation_reaching_a_second_object_is_unresolved(query):
    assert match(query).unresolved


def test_a_graphql_request_that_is_not_json_is_unresolved():
    r = Request("https", "api.linear.app", 443, "POST", "/graphql", "", {"content-type": "application/x-www-form-urlencoded"}, b"query=x")
    o = ROUTES.match(r, "/graphql")
    assert o is not None and o.unresolved

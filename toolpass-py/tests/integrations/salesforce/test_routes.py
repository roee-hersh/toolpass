"""The salesforce route table: records, creation, SOQL, refusals, and its
resources through the integration's own check. Salesforce publishes no
OpenAPI description of the REST API, so there is no description check."""

from __future__ import annotations

import json
import urllib.parse
from collections.abc import Iterator

import pytest

from tests.integrations.salesforce.test_salesforce import ACCT_ID, Env, bob, check, dana, expect
from toolpass.core.catalog import parse_resource
from toolpass.core.decision import Code
from toolpass.core.integration import find_action
from toolpass.core.routes import Request
from toolpass.integrations.salesforce import INTEGRATION
from toolpass.integrations.salesforce.actions import ACTIONS, parse_target
from toolpass.integrations.salesforce.routes import ROUTES, read_soql

V = "/services/data/v66.0"
A = "001xx000003DGb2AAG"
U = "005xx000001SvTqAAK"
JSON = "application/json"


def req(method: str, path: str, body: bytes | dict | list | None = None, query: str = "", ct: str = JSON, headers: dict | None = None) -> Request:
    data = json.dumps(body).encode() if isinstance(body, (dict, list)) else body
    h = {"content-type": ct} if ct else {}
    h.update(headers or {})
    return Request("https", "acme.my.salesforce.com", 443, method, path, query, h, data)


def soql(q: str) -> str:
    return urllib.parse.urlencode({"q": q})


def match(method: str, path: str, body: bytes | dict | list | None = None, query: str = "", **kw):
    o = ROUTES.match(req(method, path, body, query, **kw))
    assert o is not None, f"{method} {path} has no route"
    return o


def test_every_route_names_a_known_action():
    for r in ROUTES.routes:
        assert r.action is None or find_action(INTEGRATION, r.action) is not None, r
    for a in ("record.read", "record.transfer"):  # chosen by extractors
        assert find_action(INTEGRATION, a) is not None, a


@pytest.mark.parametrize(
    ("method", "path", "body", "query", "action", "resource", "effect"),
    [
        ("GET", f"{V}/sobjects/Account/{A}", None, "", "record.read", f"record:{A}", "read"),
        ("GET", f"{V}/sobjects/Account/{A}", None, "fields=Name,Industry", "record.read", f"record:{A}", "read"),
        ("GET", f"{V}/sobjects/ContentVersion/068xx000000kO7j/VersionData", None, "", "record.read", "record:068xx000000kO7j", "read"),
        ("PATCH", f"{V}/sobjects/Account/{A}", {"Name": "Acme", "NumberOfEmployees": 5}, "", "record.edit", f"record:{A}", "write"),
        ("PATCH", f"{V}/sobjects/Account/{A}", {"OwnerId": U}, "", "record.transfer", f"record:{A}", "write"),
        ("DELETE", f"{V}/sobjects/Account/{A}", None, "", "record.delete", f"record:{A}", "destructive"),
        ("POST", f"{V}/sobjects/Contact", {"LastName": "Doe", "Email": "d@example.com"}, "", "object.create", "object:Contact", "write"),
        ("POST", f"{V}/sobjects/Invoice__c", {"attributes": {"type": "Invoice__c"}, "Amount__c": 3}, "", "object.create", "object:Invoice__c", "write"),
        (
            "POST",
            f"{V}/composite/tree/Account",
            {"records": [{"attributes": {"type": "Account", "referenceId": "r1"}, "Name": "A"}, {"attributes": {"type": "Account"}, "Name": "B"}]},
            "",
            "object.create",
            "object:Account",
            "write",
        ),
        (
            "POST",
            f"{V}/composite/sobjects",
            {"allOrNone": True, "records": [{"attributes": {"type": "Lead"}, "LastName": "a"}, {"attributes": {"type": "lead"}, "LastName": "b"}]},
            "",
            "object.create",
            "object:Lead",
            "write",
        ),
        ("GET", f"{V}/sobjects/Account/describe", None, "", "object.read", "object:Account", "read"),
        ("GET", f"{V}/sobjects/Account/describe/layouts", None, "", "object.read", "object:Account", "read"),
        ("GET", f"{V}/sobjects/Account/updated", None, "start=2026-01-01T00:00:00Z", "object.view_all", "object:Account", "read"),
        ("GET", f"{V}/query", None, soql(f"SELECT Id, Name FROM Account WHERE Id = '{A}'"), "record.read", f"record:{A}", "read"),
        ("GET", f"{V}/query", None, soql(f"select name from account where id='{A}' limit 1"), "record.read", f"record:{A}", "read"),
        (
            "GET",
            f"{V}/query",
            None,
            soql("SELECT Id, Name FROM Account WHERE Industry = 'Tech' ORDER BY Name LIMIT 10"),
            "object.view_all",
            "object:Account",
            "read",
        ),
        ("GET", f"{V}/query", None, soql("SELECT COUNT(Id), Industry FROM Account GROUP BY Industry"), "object.view_all", "object:Account", "read"),
        ("GET", f"{V}/query", None, soql("SELECT FIELDS(STANDARD) FROM Lead WHERE CreatedDate = LAST_N_DAYS:30"), "object.view_all", "object:Lead", "read"),
        ("GET", f"{V}/query", None, soql("SELECT Id FROM Case WHERE CreatedDate > 2026-01-01T00:00:00Z"), "object.view_all", "object:Case", "read"),
        ("GET", f"{V}/query", None, soql(f"SELECT Id FROM Account WHERE Id = '{A}' OR Name != ''"), "object.view_all", "object:Account", "read"),
        ("GET", f"{V}/queryAll", None, soql("SELECT Id FROM Task"), "object.view_all", "object:Task", "read"),
        ("GET", f"{V}/sobjects", None, "", None, "", "read"),
        ("GET", f"{V}/limits", None, "", None, "", "read"),
        ("POST", "/services/oauth2/token", b"grant_type=x", "", None, "", "write"),
    ],
)
def test_routing(method, path, body, query, action, resource, effect):
    o = match(method, path, body, query)
    assert o.unresolved is None, o.unresolved
    assert (o.action, o.resource, o.effect) == (action, resource, effect)


@pytest.mark.parametrize(
    ("method", "path", "body", "query", "ct", "why"),
    [
        # Bodies that name another record, or that cannot be read exactly.
        ("PATCH", f"{V}/sobjects/Contact/{A}", {"AccountId": "001xx000003DGb3AAG"}, "", JSON, "another record"),
        ("POST", f"{V}/sobjects/Contact", {"LastName": "x", "AccountId": "001xx000003DGb3"}, "", JSON, "another record"),
        ("POST", f"{V}/sobjects/Contact", {"LastName": "x", "Account": {"Ext__c": "1"}}, "", JSON, "nested"),
        ("POST", f"{V}/sobjects/Account", {"Name": "x", "OwnerId": U}, "", JSON, "owned by someone else"),
        ("PATCH", f"{V}/sobjects/Account/{A}", {"Id": "001xx000003DGb3AAG"}, "", JSON, "leave Id out"),
        ("PATCH", f"{V}/sobjects/Account/{A}", {"Name": "a", "name": "b"}, "", JSON, "more than once"),
        ("PATCH", f"{V}/sobjects/Account/{A}", b"<Account><Name>x</Name></Account>", "", "application/xml", "JSON"),
        ("PATCH", f"{V}/sobjects/Account/{A}", None, "", JSON, "could not be read"),
        ("PATCH", f"{V}/sobjects/Account/{A}", {"Bad Field": 1}, "", JSON, "API name"),
        ("POST", f"{V}/sobjects/Account", {"attributes": {"type": "Contact"}, "Name": "x"}, "", JSON, "attributes"),
        ("POST", f"{V}/composite/tree/Account", {"records": [{"attributes": {"type": "Account"}, "Contacts": {"records": []}}]}, "", JSON, "nested"),
        ("POST", f"{V}/composite/tree/Account", {"records": [{"attributes": {"type": "Contact"}, "LastName": "x"}]}, "", JSON, "attributes"),
        (
            "POST",
            f"{V}/composite/sobjects",
            {"records": [{"attributes": {"type": "Lead"}, "LastName": "a"}, {"attributes": {"type": "Contact"}, "LastName": "b"}]},
            "",
            JSON,
            "several types",
        ),
        ("POST", f"{V}/composite/sobjects", {"records": [{"attributes": {"type": "Lead"}, "Id": "00Qxx0000012345"}]}, "", JSON, "leave Id out"),
        # Composite and batch requests, and other parts of the API.
        ("POST", f"{V}/composite", {"compositeRequest": []}, "", JSON, "composite"),
        ("POST", f"{V}/composite/batch", {"batchRequests": []}, "", JSON, "composite"),
        ("PATCH", f"{V}/composite/sobjects", {"records": []}, "", JSON, "composite"),
        ("DELETE", f"{V}/composite/sobjects", None, f"ids={A}", JSON, "composite"),
        ("GET", f"{V}/search", None, "q=FIND+%7Bacme%7D", JSON, "not routed"),
        ("GET", f"{V}/ui-api/records/{A}", None, "", JSON, "not routed"),
        ("GET", f"{V}/query/01gxx000000ABCD-2000", None, "", JSON, "not routed"),
        ("GET", "/services/apexrest/Accounts", None, "", JSON, "not routed"),
        ("GET", "/sfc/servlet.shepherd/version/download/068xx000000kO7j", None, "", JSON, "not routed"),
        # Records not addressed by Id.
        ("GET", f"{V}/sobjects/Account/Ext__c/12345", None, "", JSON, "not a record Id"),
        ("GET", f"{V}/sobjects/Account/{A}/Contacts", None, "", JSON, "related list"),
        ("GET", f"{V}/sobjects/Account/approvalLayouts", None, "", JSON, "not a record Id"),
        ("GET", f"{V}/sobjects/Account", None, "", JSON, "recent records"),
        ("GET", "/services/data/latest/sobjects/Account/" + A, None, "", JSON, "API version"),
        # Method overrides and paths the server may read differently.
        ("POST", f"{V}/sobjects/Account", {"Name": "x"}, "_HttpMethod=DELETE", JSON, "override"),
        ("POST", f"{V}/sobjects/Account", {"Name": "x"}, "", JSON, "override-header"),
        ("GET", f"{V}/sobjects/Account/{A};x=1", None, "", JSON, "a ; in the path"),
        ("GET", f"{V}/sobjects/Account/x/../{A}", None, "", JSON, "dot segment"),
        ("GET", f"{V}//sobjects/Account/{A}", None, "", JSON, "not routed"),
    ],
)
def test_refused(method, path, body, query, ct, why):
    headers = {"x-http-method-override": "DELETE"} if why == "override-header" else None
    o = match(method, path, body, query, ct=ct, headers=headers)
    assert o.unresolved is not None, f"{method} {path} was not refused"
    assert why.split("-")[0].lower() in o.unresolved.lower(), o.unresolved


@pytest.mark.parametrize(
    ("q", "why"),
    [
        ("SELECT Owner.Email FROM Account", "relationship field"),
        ("SELECT Id FROM Account WHERE Owner.Name = 'x'", "relationship field"),
        ("SELECT Id, (SELECT Id FROM Contacts) FROM Account", "subquery"),
        ("SELECT Id FROM Account WHERE Id IN (SELECT AccountId FROM Opportunity)", "subquery"),
        ("SELECT TYPEOF What WHEN Account THEN Name END FROM Event", "TYPEOF"),
        ("SELECT Id FROM Account FOR UPDATE", "FOR"),
        ("SELECT Id FROM KnowledgeArticleVersion UPDATE VIEWSTAT", "UPDATE"),
        ("SELECT Id FROM Account USING SCOPE Everything", "USING"),
        ("SELECT Id FROM Account WITH SECURITY_ENFORCED", "WITH"),
        ("SELECT Id FROM Account a", "follow the object"),
        ("SELECT Id FROM Contact c, c.Account a", "relationship field"),
        ("SELECT Id FROM Account WHERE Name = 'it\\'s' FROM Contact", "one FROM"),
        ("SELECT Id FROM Account WHERE Name = 'x''", "adjacent"),
        ("SELECT Id FROM Account WHERE Name = 'x", "unterminated"),
        ("SELECT Id FROM Account; DELETE", "character"),
        ('SELECT Id FROM Account WHERE Name = "x"', "character"),
        ("DELETE FROM Account", "start with SELECT"),
        ("SELECT FROM Account", "fields FROM one object"),
        ("SELECT Id FROM 1Account", "not a number"),
        ("SELECT Id, 'x' FROM Account", "string in the field list"),
    ],
)
def test_soql_the_parser_cannot_read_is_refused(q, why):
    with pytest.raises(ValueError, match="(?i)" + why.replace("(", r"\(")):
        read_soql(q)
    o = match("GET", f"{V}/query", None, soql(q))
    assert o.unresolved is not None


def test_soql_strings_hide_nothing():
    # A keyword inside a string is text, not a clause; an escaped quote does not end the string.
    assert read_soql("SELECT Id FROM Account WHERE Name = 'x FROM Contact WHERE Id = \\' SELECT'") == ("object", "Account")
    assert read_soql(f"SELECT Id FROM Account WHERE Id = '{A}' ") == ("record", A)
    assert read_soql("SELECT Id FROM Account WHERE Id = 'Internationally'") == ("object", "Account")


def test_query_parameters():
    for query in (
        "",
        soql("SELECT Id FROM Account") + "&q=SELECT+Id+FROM+Contact",
        soql("SELECT Id FROM Account") + "&explain=x",
        "q=SELECT+Id%00+FROM+Account",
    ):
        o = match("GET", f"{V}/query", None, query)
        assert o.unresolved is not None, query


def test_unrouted_writes_have_no_route():
    for method, path in (
        ("PATCH", f"{V}/sobjects/Account/Ext__c/12345"),
        ("POST", f"{V}/sobjects/Account/{A}"),
        ("POST", f"{V}/actions/standard/emailSimple"),
        ("POST", "/services/apexrest/Accounts"),
        ("POST", f"{V}/jobs/ingest"),
    ):
        assert ROUTES.match(req(method, path, {})) is None, path
    assert ROUTES.is_unrouted("PATCH", f"{V}/sobjects/Account/Ext__c/12345") is not None
    assert ROUTES.is_unrouted("POST", f"{V}/actions/standard/emailSimple") is not None


def test_resources_parse_with_the_integrations_parser():
    for method, path, body, query in (
        ("GET", f"{V}/sobjects/Account/{A}", None, ""),
        ("PATCH", f"{V}/sobjects/Account/{A}", {"OwnerId": U}, ""),
        ("DELETE", f"{V}/sobjects/Account/{A}", None, ""),
        ("POST", f"{V}/sobjects/Invoice__c", {"Amount__c": 1}, ""),
        ("POST", f"{V}/composite/sobjects", {"records": [{"attributes": {"type": "Lead"}, "LastName": "a"}]}, ""),
        ("GET", f"{V}/query", None, soql("SELECT Id FROM Account")),
        ("GET", f"{V}/query", None, soql(f"SELECT Id FROM Account WHERE Id = '{A}'")),
        ("GET", f"{V}/sobjects/Account/describe", None, ""),
    ):
        o = match(method, path, body, query)
        assert o.action is not None and o.resource is not None, path
        parse_target(ACTIONS[o.action], parse_resource(o.resource))


@pytest.fixture
def env() -> Iterator[Env]:
    e = Env()
    yield e
    e.close()


def test_routed_requests_through_the_real_check(env: Env) -> None:
    _, c = env.setup()

    def decide(user, method, path, body=None, query=""):
        o = match(method, path, body, query)
        assert o.unresolved is None, o.unresolved
        return check(c, user, o.action, o.resource)

    expect(decide(dana, "GET", f"{V}/sobjects/Account/{ACCT_ID}"), Code.ALLOWED, "HasReadAccess")
    expect(decide(dana, "GET", f"{V}/query", None, soql(f"SELECT Name FROM Account WHERE Id = '{ACCT_ID}'")), Code.ALLOWED, "HasReadAccess")
    expect(decide(bob, "POST", f"{V}/sobjects/Account", {"Name": "x"}), Code.DENIED, "PermissionsCreate")
    expect(decide(bob, "GET", f"{V}/query", None, soql("SELECT Name FROM Account")), Code.DENIED, "PermissionsViewAllRecords")


def test_endpoint_from_the_url_setting():
    s = type("S", (), {"get": lambda self, k: "https://acme.my.salesforce.com"})()
    assert ROUTES.endpoints(s) == [("acme.my.salesforce.com", "")]

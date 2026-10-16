"""The snowflake route table: one SQL statement in a JSON body, read into one privilege on one table."""

from __future__ import annotations

import gzip
import json
from collections.abc import Iterator
from typing import Any

import pytest

from tests.harness.routes import check_routes_against_spec
from tests.integrations.snowflake.test_snowflake import Env, check, dana, expect
from toolpass.core.catalog import parse_resource
from toolpass.core.decision import Code
from toolpass.core.integration import Settings, find_action
from toolpass.core.routes import Request
from toolpass.integrations.snowflake import INTEGRATION
from toolpass.integrations.snowflake.actions import parse_target
from toolpass.integrations.snowflake.routes import ACTIONS_ROUTED, ROUTES, read_statement

HOST = "myorg-myaccount.snowflakecomputing.com"
JSON = {"content-type": "application/json"}


def sqlapi(statement: str, **fields: Any) -> Request:
    body = json.dumps({"statement": statement, **fields}).encode()
    return Request("https", HOST, 443, "POST", "/api/v2/statements", "", JSON, body)


def connector(sql: str, *, compress: bool = True, **fields: Any) -> Request:
    body = json.dumps({"sqlText": sql, "asyncExec": False, "sequenceId": 1, "querySubmissionTime": 1700000000000, **fields}).encode()
    headers = dict(JSON)
    if compress:
        body, headers["content-encoding"] = gzip.compress(body), "gzip"
    return Request("https", HOST, 443, "POST", "/queries/v1/query-request", "requestId=1", headers, body)


def match(r: Request):
    o = ROUTES.match(r)
    assert o is not None
    return o


def test_routes_match_the_api_description():
    assert check_routes_against_spec(ROUTES, "snowflake-sqlapi", host=HOST) == []


def test_every_action_is_one_the_integration_checks():
    for name in {*ACTIONS_ROUTED, *(r.action for r in ROUTES.routes if r.action)}:
        assert find_action(INTEGRATION, name) is not None, name


def test_the_account_is_where_the_api_lives():
    assert ROUTES.endpoints(Settings("sf", "snowflake", {"account": "myorg-myaccount"})) == [(HOST, "")]
    # Underscores are hyphens in the host name the integration builds; the connector uses the account as given.
    assert ROUTES.endpoints(Settings("sf", "snowflake", {"account": "MyOrg-My_Account"})) == [
        ("myorg-my-account.snowflakecomputing.com", ""),
        ("myorg-my_account.snowflakecomputing.com", ""),
    ]
    assert ROUTES.endpoints(Settings("sf", "snowflake", {"account": "x", "url": "https://acme.privatelink.snowflakecomputing.com"})) == [
        ("acme.privatelink.snowflakecomputing.com", "")
    ]


SQLAPI_CASES = [
    ("select * from prod.sales.orders", {}, "table.select", "table:PROD.SALES.ORDERS", "read"),
    ("SELECT a, COUNT(*) AS n FROM PROD.SALES.ORDERS o WHERE o.b IN (1, 2) GROUP BY a HAVING n > 1 ORDER BY 2 DESC LIMIT 10", {},
     "table.select", "table:PROD.SALES.ORDERS", "read"),
    ("select id from orders where created > dateadd(day, -7, current_date()) qualify row_number() over (partition by id order by created) = 1",
     {"database": "PROD", "schema": "SALES"}, "table.select", "table:PROD.SALES.ORDERS", "read"),
    ("select x:a.b::string, cast(y as number(10, 2)) from sales.orders", {"database": "PROD"}, "table.select", "table:PROD.SALES.ORDERS", "read"),
    ('select * from PROD.SALES."ORDERS 2024"', {}, "table.select", 'table:PROD.SALES."ORDERS 2024"', "read"),
    ("select * from prod.sales.orders where id = ? and name = :name", {"bindings": {"1": {"type": "FIXED", "value": "1"}}},
     "table.select", "table:PROD.SALES.ORDERS", "read"),
    ("select 1", {}, None, "", "read"),
    ("insert into prod.sales.orders (id, note) values (1, 'it''s'), (2, null)", {}, "table.insert", "table:PROD.SALES.ORDERS", "write"),
    ("insert into orders values (?, ?)", {"database": "PROD", "schema": "SALES"}, "table.insert", "table:PROD.SALES.ORDERS", "write"),
    ("update prod.sales.orders set note = upper(note), n = n + 1 where id = 3", {}, "table.update", "table:PROD.SALES.ORDERS", "write"),
    ("delete from prod.sales.orders where id = 3", {}, "table.delete", "table:PROD.SALES.ORDERS", "destructive"),
    ("delete from prod.sales.orders", {}, "table.delete", "table:PROD.SALES.ORDERS", "destructive"),
    ("truncate table if exists prod.sales.orders", {}, "table.truncate", "table:PROD.SALES.ORDERS", "destructive"),
    ("select * from prod.sales.orders", {"parameters": {"query_tag": "agent", "MULTI_STATEMENT_COUNT": 1}, "warehouse": "WH", "timeout": 60},
     "table.select", "table:PROD.SALES.ORDERS", "read"),
    # A fully qualified name wins over the defaults.
    ("select * from dev.play.t", {"database": "PROD", "schema": "SALES"}, "table.select", "table:DEV.PLAY.T", "read"),
    # Keywords and types before "(" where they cannot be a call.
    ("select * exclude (a, b) rename (c as d) from prod.sales.orders", {}, "table.select", "table:PROD.SALES.ORDERS", "read"),
    ("select o.* rename (c as d), try_cast(x as timestamp_ntz(9)), y::varchar(10) from prod.sales.orders o", {},
     "table.select", "table:PROD.SALES.ORDERS", "read"),
    ("select listagg(a, ',') within group (order by a) over (partition by b), sum(c) over (order by a) from prod.sales.orders", {},
     "table.select", "table:PROD.SALES.ORDERS", "read"),
]  # fmt: skip


@pytest.mark.parametrize(("statement", "fields", "action", "resource", "effect"), SQLAPI_CASES)
def test_sql_api_statements(statement, fields, action, resource, effect):
    o = match(sqlapi(statement, **fields))
    assert o.unresolved is None, o.unresolved
    assert (o.action, o.resource, o.effect) == (action, resource, effect)
    assert o.untrusted_output == (effect == "read")


@pytest.mark.parametrize(("statement", "fields", "action", "resource", "effect"), [c for c in SQLAPI_CASES if not c[1]])
def test_the_connector_reads_the_same_statements(statement, fields, action, resource, effect):
    for compress in (True, False):
        o = match(connector(statement, compress=compress))
        assert o.unresolved is None, o.unresolved
        assert (o.action, o.resource, o.effect) == (action, resource, effect)


@pytest.mark.parametrize(
    ("statement", "fields"),
    [
        # More than one statement.
        ("select * from prod.sales.orders; delete from prod.sales.orders", {}),
        ("select * from prod.sales.orders;", {}),
        ("select * from prod.sales.orders", {"parameters": {"MULTI_STATEMENT_COUNT": 2}}),
        ("select * from prod.sales.orders", {"parameters": {"multi_statement_count": 0}}),
        ("select * from prod.sales.orders ->> select * from prod.hr.salaries", {}),
        # Comments and quoting toolpass does not read the way Snowflake does.
        ("select * from prod.sales.orders -- where 1", {}),
        ("select * from prod.sales.orders // x", {}),
        ("select * /* x */ from prod.sales.orders", {}),
        ("select 'a\\' from prod.hr.salaries' from prod.sales.orders", {}),
        ('select * from prod.sales."orders"', {}),
        ("select $$x$$ from prod.sales.orders", {}),
        ("select * from prod.sales.orders where a = $var", {}),
        ("select * from @stage", {}),
        ("select * from prod.sales.orders ", {}),
        # A second object: joins, subqueries, set operations, CTEs, table functions, sequences.
        ("select * from prod.sales.orders o join prod.hr.salaries s on o.id = s.id", {}),
        ("select * from prod.sales.orders, prod.hr.salaries", {}),
        ("select * from prod.sales.orders where id in (select id from prod.hr.salaries)", {}),
        ("select (select max(pay) from prod.hr.salaries) from prod.sales.orders", {}),
        ("select * from prod.sales.orders union select * from prod.hr.salaries", {}),
        ("with s as (select * from prod.hr.salaries) select * from s", {}),
        ("select * from table(result_scan(last_query_id()))", {}),
        ("select * from identifier('prod.hr.salaries')", {}),
        ("select * from identifier(?)", {"bindings": {"1": {"type": "TEXT", "value": "prod.hr.salaries"}}}),
        ("select prod.sales.seq.nextval from prod.sales.orders", {}),
        ("select * from prod.sales.orders at(offset => -60)", {}),
        ("select * from prod.sales.orders sample (10)", {}),
        ("select * from prod.sales.orders o, lateral flatten(input => o.items)", {}),
        # Functions that may read other objects.
        ("select get_ddl('table', 'prod.hr.salaries')", {}),
        ("select system$whitelist()", {}),
        ("select my_udf(id) from prod.sales.orders", {}),
        ("select prod.util.f(id) from prod.sales.orders", {}),
        ('select "UPPER"(id) from prod.sales.orders', {}),
        # Keywords and type names that would be a call to a function of that name where they stand.
        ("select range(1, 5) from prod.sales.orders", {}),
        ("select rename(id) from prod.sales.orders", {}),
        ("select exclude(id) from prod.sales.orders", {}),
        ("select 2 * exclude(id) from prod.sales.orders", {}),
        ("select id, within(id) from prod.sales.orders", {}),
        ("select over(id) from prod.sales.orders", {}),
        ("select (id) over (partition by id) from prod.sales.orders", {}),
        ("select text(id) from prod.sales.orders", {}),
        ("select string(id) from prod.sales.orders", {}),
        ("select timestamp_ntz(id) from prod.sales.orders", {}),
        ("select * from prod.sales.orders where number(id) = 1", {}),
        ("select coalesce(a as number(5)) from prod.sales.orders", {}),
        ("update prod.sales.orders set a = varchar(b)", {}),
        ("insert into prod.sales.orders values (decimal(1))", {}),
        ("select rows(1), preceding(1), following(1) from prod.sales.orders", {}),
        # Writes reaching beyond one table.
        ("insert into prod.sales.orders select * from prod.hr.salaries", {}),
        ("insert overwrite into prod.sales.orders values (1)", {}),
        ("insert all into prod.sales.a into prod.sales.b select 1", {}),
        ("insert into prod.sales.orders values (1) , ", {}),
        ("update prod.sales.orders o set note = s.pay from prod.hr.salaries s where o.id = s.id", {}),
        ("delete from prod.sales.orders using prod.hr.salaries where 1 = 1", {}),
        ("merge into prod.sales.orders using prod.hr.salaries on 1 = 1 when matched then delete", {}),
        ("truncate table prod.sales.orders, prod.hr.salaries", {}),
        # Statements toolpass does not read.
        ("drop table prod.sales.orders", {}),
        ("create table prod.sales.t as select * from prod.hr.salaries", {}),
        ("grant select on prod.hr.salaries to role public", {}),
        ("call prod.util.proc()", {}),
        ("execute immediate 'select 1'", {}),
        ("show tables", {}),
        ("use database prod", {}),
        # Names toolpass cannot place.
        ("select * from orders", {}),
        ("select * from orders", {"database": "PROD"}),
        ("select * from sales.orders", {"schema": "SALES"}),
        ("select * from orders", {"database": "prod", "schema": "SALES"}),
        ("select * from orders", {"database": '"PROD"', "schema": "SALES"}),
        ("select * from a.b.c.d", {}),
        ("select * from prod..orders", {}),
        # Body fields and parameters toolpass does not know.
        ("select * from prod.sales.orders", {"parameters": {"QUOTED_IDENTIFIERS_IGNORE_CASE": True}}),
        ("select * from prod.sales.orders", {"parameters": {"search_path": "PROD.HR"}}),
        ("select * from prod.sales.orders", {"Statement": "delete from prod.sales.orders"}),
        ("select * from prod.sales.orders", {"bindings": [1]}),
        ("", {}),
    ],
)
def test_statements_toolpass_cannot_read_exactly_are_refused(statement, fields):
    o = match(sqlapi(statement, **fields))
    assert o.unresolved, (statement, o)


def test_bodies_toolpass_cannot_read_exactly_are_refused():
    def raw(body: bytes, headers: dict[str, str] | None = None) -> Request:
        return Request("https", HOST, 443, "POST", "/api/v2/statements", "", headers or JSON, body)

    # A member given twice: Snowflake may take the other one.
    assert match(raw(b'{"statement": "select * from prod.sales.orders", "statement": "delete from prod.sales.orders"}')).unresolved
    assert match(raw(b'{"statement": "select 1", "database": "PROD", "database": "HR", "schema": "S"}')).unresolved
    assert match(raw(b'{"statement": "select 1", "timeout": NaN}')).unresolved
    assert match(raw(b"[]")).unresolved
    assert match(raw(b"")).unresolved
    assert match(raw(b'{"statement": "select 1"}', {"content-type": "text/plain"})).unresolved
    assert match(raw(b"\xef\xbb\xbf" + b'{"statement": "select 1"}')).unresolved


def test_the_connector_needs_fully_qualified_names_and_known_fields():
    # The session's database and schema are set at login, not in the body.
    assert match(connector("select * from orders")).unresolved
    assert match(connector("select * from sales.orders")).unresolved
    assert match(connector("select * from prod.sales.orders", bindStage="@stage/x")).unresolved
    assert match(connector("select * from prod.sales.orders", dataframeAst="...")).unresolved
    assert match(connector("select * from prod.sales.orders", parameters={"MULTI_STATEMENT_COUNT": 0})).unresolved
    o = match(connector("select * from prod.sales.orders", parameters={"QUERY_TAG": "x"}, bindings=None, queryContextDTO={"entries": []}))
    assert (o.action, o.resource, o.unresolved) == ("table.select", "table:PROD.SALES.ORDERS", None)
    for empty in ({}, None):
        assert match(connector("select * from prod.sales.orders", queryContextDTO=empty, describedJobId=None)).unresolved is None
    # The query context the connector echoes after its first query is accepted; it runs nothing.
    context = {"entries": [{"id": 0, "timestamp": 1, "priority": 0, "context": {"base64Data": "AAAA"}}]}
    assert match(connector("select * from prod.sales.orders", queryContextDTO=context)).unresolved is None
    # Another statement's job is refused: the server could run its plan instead of this text.
    assert match(connector("select * from prod.sales.orders", describedJobId="01b0f0f0-0000-0000-0000-000000000002")).unresolved
    assert match(connector("select * from prod.sales.orders", queryContextDTO="x")).unresolved
    bad = Request("https", HOST, 443, "POST", "/queries/v1/query-request", "", {**JSON, "content-encoding": "br"}, b"{}")
    assert match(bad).unresolved
    truncated = gzip.compress(json.dumps({"sqlText": "select 1"}).encode())[:-4]
    assert match(Request("https", HOST, 443, "POST", "/queries/v1/query-request", "", {**JSON, "content-encoding": "gzip"}, truncated)).unresolved


def test_results_by_handle_are_refused():
    # The agent's credential is shared: a handle may be any statement's, not one toolpass checked.
    handle = "01b0f0f0-0000-0000-0000-000000000002"
    for path, query in ((f"/api/v2/statements/{handle}", "partition=1"), (f"/api/v2/statements/{handle}", ""),
                        (f"/queries/{handle}/result", ""), (f"/monitoring/queries/{handle}", "")):  # fmt: skip
        o = match(Request("https", HOST, 443, "GET", path, query))
        assert o.unresolved and "handle" in o.unresolved and o.action is None, path


def test_the_session_and_cancelling_need_no_check():
    handle = "01b0f0f0-0000-0000-0000-000000000002"
    o = match(Request("https", HOST, 443, "POST", f"/api/v2/statements/{handle}/cancel", "", JSON, b""))
    assert (o.action, o.effect, o.unresolved) == (None, "write", None)
    for path in ("/session/v1/login-request", "/session/token-request", "/session/heartbeat", "/queries/v1/abort-request"):
        o = match(Request("https", HOST, 443, "POST", path, "", JSON, b"{}"))
        assert (o.action, o.unresolved) == (None, None), path


def test_other_snowflake_writes_have_no_route():
    assert ROUTES.match(Request("https", HOST, 443, "POST", "/api/v2/databases", "", JSON, b"{}")) is None


@pytest.mark.parametrize(("statement", "fields", "action", "resource", "effect"), [c for c in SQLAPI_CASES if c[2]])
def test_resources_parse_as_the_integration_reads_them(statement, fields, action, resource, effect):
    t = parse_target(action, parse_resource(resource))
    assert t.kind == "table" and len(t.name) == 3


def test_a_quoted_name_resolves_as_snowflake_resolves_it():
    _, resource, _ = read_statement('select * from "PROD".sales."A.B ""C"""')
    assert resource == 'table:PROD.SALES."A.B ""C"""'
    assert parse_target("table.select", parse_resource(resource)).name == ("PROD", "SALES", 'A.B "C"')


@pytest.fixture
def sf() -> Iterator[Env]:
    e = Env()
    yield e
    e.close()


def test_routed_statements_are_checked_on_the_users_grants(sf: Env):
    _, _, c = sf.setup()

    def decide(statement: str, **fields: Any) -> Code:
        o = match(sqlapi(statement, **fields))
        assert o.unresolved is None and o.action and o.resource, o
        return check(c, dana, o.action, o.resource).code

    # dana (ANALYST) may read PROD.SALES.ORDERS, not write it, and may not read PROD.HR.SALARIES (no USAGE on the schema).
    assert decide("select * from orders", database="PROD", schema="SALES") == Code.ALLOWED
    assert decide("select count(*) from prod.sales.v_orders") == Code.ALLOWED
    assert decide("delete from prod.sales.orders where id = 1") == Code.DENIED
    assert decide("insert into sales.orders values (1)", database="PROD") == Code.DENIED
    assert decide("select * from prod.hr.salaries") == Code.DENIED
    o = match(sqlapi("select * from salaries", database="PROD", schema="HR"))
    assert o.action and o.resource
    expect(check(c, dana, o.action, o.resource), Code.DENIED, 'no role holds USAGE on schema "PROD"."HR"')

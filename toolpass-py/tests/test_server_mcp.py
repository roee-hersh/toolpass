"""``POST /mcp``: the permission tools, spoken to as an MCP client speaks
Streamable HTTP, over a real socket. The engine is a real in-process one over
the ``fake`` integration unless a test needs to see what reached it.
``tests/frameworks/test_mcp.py`` drives the same endpoint with the official client."""

from __future__ import annotations

import http.client
import io
import json
from collections.abc import Iterator
from typing import Any

import pytest

import toolpass
from toolpass._mcp_endpoint import PROTOCOL_VERSIONS
from toolpass.core import secret
from toolpass.core.context import Context
from toolpass.core.decision import allowed
from toolpass.core.engine import Request, Result
from toolpass.core.log import JSONHandler, Logger
from toolpass.server import MAX_REQUEST_BODY, Server

KEY = "CANARY-SECRET-apikey"
ILYA, DANA = "ilya@example.com", "dana@example.com"
CONNECTIONS = [
    {"id": "things", "integration": "fake", "users": ILYA, "admins": DANA},
    {"id": "down", "integration": "fake", "users": ILYA, "fail": "upstream_timeout"},
    {"id": "k8s", "integration": "kubernetes", "url": "https://10.20.0.5:6443", "credential": toolpass.literal("t")},
]


class Recorder:
    """A checker that allows everything and keeps the request."""

    def __init__(self) -> None:
        self.last: Request | None = None

    def check(self, ctx: Context | None, req: Request) -> Result:
        self.last = req
        return Result(allowed("ok"), 200)


class Running:
    def __init__(self, checker: Any, logger: Logger | None = None) -> None:
        self.server = Server(checker, secret.literal(KEY), logger)
        self.host, self.port = self.server.listen("127.0.0.1:0")
        self.server.serve_in_thread()

    def send(self, body: bytes, method: str = "POST", headers: dict[str, str] | None = None, key: str | None = KEY) -> tuple[http.client.HTTPResponse, bytes]:
        h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream", **(headers or {})}
        if key is not None:
            h["Authorization"] = "Bearer " + key
        conn = http.client.HTTPConnection(self.host, self.port, timeout=5)
        try:
            conn.request(method, "/mcp", body=body, headers=h)
            res = conn.getresponse()
            return res, res.read()
        finally:
            conn.close()

    def rpc(self, method: str, params: dict[str, Any] | None = None, headers: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
        msg: dict[str, Any] = {"jsonrpc": "2.0", "id": 7, "method": method}
        if params is not None:
            msg["params"] = params
        res, body = self.send(json.dumps(msg).encode(), headers=headers)
        assert res.getheader("Content-Type") == "application/json" and res.getheader("Mcp-Session-Id") is None
        out = json.loads(body)
        assert out["jsonrpc"] == "2.0" and out["id"] == 7, out
        return res.status, out

    def call(self, tool: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        status, out = self.rpc("tools/call", {"name": tool, **({} if arguments is None else {"arguments": arguments})})
        assert status == 200 and "result" in out, out
        result: dict[str, Any] = out["result"]
        if "structuredContent" in result:
            assert json.loads(result["content"][0]["text"]) == result["structuredContent"], "the text is the same object"
        return result


@pytest.fixture
def started() -> Iterator[list[Running]]:
    servers: list[Running] = []
    yield servers
    for r in servers:
        r.server.shutdown()


@pytest.fixture
def mcp(started: list[Running]) -> Running:
    r = Running(toolpass.Toolpass(connections=CONNECTIONS)._backend.engine)  # type: ignore[attr-defined]
    started.append(r)
    return r


def check(mcp: Running, user: str, action: str = "thing.write", connection: str = "things", **more: Any) -> dict[str, Any]:
    return mcp.call("check_permission", {"user": user, "connection": connection, "action": action, "resource": "thing:1", **more})


def test_initialize(mcp: Running) -> None:
    for asked, got in (
        (PROTOCOL_VERSIONS[-1], PROTOCOL_VERSIONS[-1]),
        ("2025-06-18", "2025-06-18"),
        ("2099-01-01", PROTOCOL_VERSIONS[0]),
        (None, PROTOCOL_VERSIONS[0]),
    ):
        status, out = mcp.rpc("initialize", {"protocolVersion": asked, "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}})
        r = out["result"]
        assert (status, r["protocolVersion"]) == (200, got), out
        assert r["capabilities"] == {"tools": {}} and r["serverInfo"]["name"] == "toolpass" and r["serverInfo"]["version"] == toolpass.__version__
        assert "not a 'no'" in r["instructions"] and "does not stop" in r["instructions"]
    assert mcp.rpc("ping") == (200, {"jsonrpc": "2.0", "id": 7, "result": {}})


def test_notifications_and_responses_are_accepted_without_a_body(mcp: Running) -> None:
    for msg in (
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 3, "result": {}},
        {"jsonrpc": "2.0", "id": 3, "error": {"code": 1, "message": "x"}},
    ):
        res, body = mcp.send(json.dumps(msg).encode())
        assert (res.status, body) == (202, b""), msg


def test_tools_are_listed_read_only(mcp: Running) -> None:
    status, out = mcp.rpc("tools/list", headers={"MCP-Protocol-Version": "2025-11-25"})
    tools = {t["name"]: t for t in out["result"]["tools"]}
    assert status == 200 and list(tools) == ["list_connections", "list_actions", "check_permission"]
    for t in tools.values():
        assert t["annotations"]["readOnlyHint"] is True and t["annotations"]["destructiveHint"] is False
        assert t["inputSchema"]["type"] == "object" and t["outputSchema"]["type"] == "object" and t["description"]
    assert tools["check_permission"]["inputSchema"]["required"] == ["user", "connection", "action", "resource"]
    assert "token" not in tools["check_permission"]["inputSchema"]["properties"], "a user's ID token never goes through a model"


def test_list_connections_and_actions(mcp: Running) -> None:
    assert mcp.call("list_connections")["structuredContent"] == {
        "connections": [{"id": "things", "integration": "fake"}, {"id": "down", "integration": "fake"}, {"id": "k8s", "integration": "kubernetes"}]
    }
    r = mcp.call("list_actions", {"connection": "things"})
    assert r["isError"] is False and r["structuredContent"]["integration"] == "fake"
    assert r["structuredContent"]["actions"][1] == {"name": "thing.write", "description": "write a thing (admins only)"}
    k8s = {a["name"]: a for a in mcp.call("list_actions", {"connection": "k8s"})["structuredContent"]["actions"]}
    assert k8s["deployment.create"] == {"name": "deployment.create", "description": "create a deployment (create deployments.apps)"}
    assert k8s["raw:<verb>"]["pattern"] is True
    r = mcp.call("list_actions", {"connection": "nope"})
    assert r["isError"] is True and "structuredContent" not in r and "list_connections" in r["content"][0]["text"]


def test_check_permission_three_answers(mcp: Running) -> None:
    r = check(mcp, DANA)
    assert r["isError"] is False
    assert r["structuredContent"] == {"decision": "allow", "reason": f"allowed: {DANA} is an admin", "code": "allowed", "identity": DANA}
    r = check(mcp, ILYA)
    assert (r["isError"], r["structuredContent"]["decision"], r["structuredContent"]["code"]) == (False, "deny", "denied")
    r = check(mcp, "nobody@example.com")
    assert (r["isError"], r["structuredContent"]["decision"], r["structuredContent"]["code"]) == (False, "deny", "user_not_found")
    assert "identity" not in r["structuredContent"]
    # A system that is down is "unknown", an answer (not an error), and never a deny.
    r = check(mcp, ILYA, connection="down")
    assert (r["isError"], r["structuredContent"]["decision"], r["structuredContent"]["code"]) == (False, "unknown", "upstream_timeout")


def test_a_question_that_was_not_asked_is_an_error_the_model_can_fix(mcp: Running) -> None:
    for kwargs, code in (
        ({"user": "ilya"}, "invalid_request"),
        ({"user": ILYA, "action": "nope"}, "unknown_action"),
        ({"user": ILYA, "connection": "nope"}, "unknown_connection"),
    ):
        r = check(mcp, **kwargs)  # type: ignore[arg-type]
        assert (r["isError"], r["structuredContent"]["decision"], r["structuredContent"]["code"]) == (True, "unknown", code), r
    # The integration's own refusal of a resource names the shapes it takes.
    r = mcp.call("check_permission", {"user": ILYA, "connection": "k8s", "action": "deployment.create", "resource": "bogus:1"})
    assert r["isError"] is True and r["structuredContent"]["code"] == "invalid_request"
    assert "use namespace:<ns>, cluster or nonresource:<path>" in r["structuredContent"]["reason"]
    for arguments, words in (
        ({"user": ILYA}, "missing arguments: connection, action, resource"),
        ({"user": ILYA, "connection": "things", "action": "thing.read"}, "missing argument: resource"),
        ({"user": ILYA, "connection": "things", "action": "thing.read", "resource": None}, "missing argument: resource"),
        ({"user": 7, "connection": "things", "action": "thing.read", "resource": "thing:1"}, "argument 'user' must be a string"),
        ({"user": ILYA, "connection": "things", "action": "thing.read", "resource": "thing:1", "fresh": "yes"}, "argument 'fresh' must be a boolean"),
        (
            {"user": ILYA, "connection": "things", "action": "thing.read", "resource": "thing:1", "groups": "admins"},
            "argument 'groups' must be a list of strings",
        ),
        ({"user": ILYA, "connection": "things", "action": "thing.read", "resource": "thing:1", "token": "eyJ"}, "unknown argument 'token'"),
    ):
        r = mcp.call("check_permission", arguments)
        assert r["isError"] is True and "structuredContent" not in r and words in r["content"][0]["text"], r
    status, out = mcp.rpc("tools/call", {"name": "check_permission", "arguments": [ILYA]})
    assert status == 200 and out["result"]["isError"] is True
    assert mcp.call("list_connections", {"x": 1})["content"][0]["text"] == "unknown argument 'x'; this tool takes: no arguments"


def test_arguments_reach_the_engine(started: list[Running]) -> None:
    logs = io.StringIO()
    rec = Recorder()
    r = Running(rec, Logger(JSONHandler(logs)))
    started.append(r)
    assert r.call("list_connections")["structuredContent"] == {"connections": []}, "an engine that cannot name its connections"
    out = r.call("check_permission", {"user": "a@x.com", "connection": "c", "action": "thing.read", "resource": "thing:1"})
    assert out["structuredContent"] == {"decision": "allow", "reason": "allowed: ok", "code": "allowed"}
    assert rec.last is not None
    assert (rec.last.user, rec.last.connection, rec.last.action, rec.last.resource) == ("a@x.com", "c", "thing.read", "thing:1")
    assert (rec.last.groups, rec.last.fresh, rec.last.token, rec.last.remote) == ([], False, "", "127.0.0.1")
    r.call("check_permission", {"user": "a\ud800@x.com", "connection": "c", "action": "a", "resource": "r", "groups": ["g1", "g2"], "fresh": True})
    assert (rec.last.user, rec.last.groups, rec.last.fresh) == ("a�@x.com", ["g1", "g2"], True), "a lone surrogate never reaches the engine"
    text = logs.getvalue()
    assert '"tool":"check_permission"' in text and '"path":"/mcp"' in text
    assert KEY not in text and "a@x.com" not in text, f"log leaked: {text}"


def test_the_api_key_comes_first(mcp: Running) -> None:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode()
    for key in (None, "wrong", ""):
        for method in ("POST", "GET", "DELETE"):
            res, out = mcp.send(body if method == "POST" else b"", method=method, key=key)
            assert res.status == 401 and res.getheader("WWW-Authenticate") == 'Bearer realm="toolpass"', (key, method)
            assert json.loads(out) == {"error": "missing or wrong API key"}
    res, _ = mcp.send(b"x" * (MAX_REQUEST_BODY + 1), key=None)
    assert res.status == 401, "the key is checked before the size"


def test_only_post(mcp: Running) -> None:
    # No stream to open and no session to end: a client is told so and carries on.
    for method in ("GET", "DELETE", "PUT"):
        res, _ = mcp.send(b"", method=method)
        assert (res.status, res.getheader("Allow")) == (405, "POST"), method


def test_bodies_that_are_not_one_request(mcp: Running) -> None:
    def error(body: bytes) -> tuple[int, Any, int]:
        res, out = mcp.send(body)
        e = json.loads(out)
        return res.status, e["id"], e["error"]["code"]

    assert error(b"{") == (400, None, -32700)
    assert error(b"") == (400, None, -32700)
    assert error(b"[" * 50000) == (400, None, -32700)
    assert error(b'[{"jsonrpc":"2.0","id":1,"method":"ping"}]') == (400, None, -32600), "a batch"
    assert error(b'{"id":1,"method":"ping"}') == (400, None, -32600)
    assert error(b'{"jsonrpc":"2.0"}') == (400, None, -32600)
    assert error(b'{"jsonrpc":"2.0","id":null,"method":"ping"}') == (400, None, -32600)
    assert error(b'{"jsonrpc":"2.0","id":true,"method":"ping"}') == (400, None, -32600)
    assert error(b'{"jsonrpc":"2.0","id":1,"method":7}') == (400, 1, -32600)
    assert error(b'{"jsonrpc":"2.0","id":1,"method":"ping","method":"tools/list"}') == (400, None, -32600), "a field given twice"
    dup = b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"check_permission","arguments":{"user":"a@x.com","user":"b@x.com"}}}'
    assert error(dup) == (400, None, -32600), "two users: a proxy and this server could disagree about who is asked about"
    assert error(b'{"jsonrpc":"2.0","id":1,"method":"ping","params":[]}') == (200, 1, -32602)
    assert error(b'{"jsonrpc":"2.0","id":"a","method":"resources/list"}') == (200, "a", -32601)
    assert error(b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"delete_everything"}}') == (200, 1, -32602)
    assert error(b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{}}') == (200, 1, -32602)
    res, out = mcp.send(b"x" * (MAX_REQUEST_BODY + 1))
    assert res.status == 413 and str(MAX_REQUEST_BODY) in json.loads(out)["error"]


def test_protocol_version_header(mcp: Running) -> None:
    for v in (*PROTOCOL_VERSIONS, ""):
        assert mcp.rpc("ping", headers={"MCP-Protocol-Version": v} if v else None)[0] == 200, v
    # A newer client's first probe: told which revisions are served, it falls back to initialize.
    status, out = mcp.rpc("server/discover", headers={"MCP-Protocol-Version": "2026-07-28"})
    assert status == 400 and out["error"]["code"] == -32022
    assert out["error"]["data"] == {"supported": list(PROTOCOL_VERSIONS), "requested": "2026-07-28"}
    assert mcp.rpc("server/discover")[1]["error"]["code"] == -32601

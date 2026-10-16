"""HTTP MCP servers through the gateway: `gateway on` wraps one that answers to
the entry's own headers, `toolpass mcp -url` carries every message to it and
back (JSON and event-stream answers alike), the tool calls land in the record,
and `off` puts the entry back as it was."""

from __future__ import annotations

import io
import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from toolpass import _gateway, _mcp_http
from toolpass._gateway import ClientFile
from toolpass._store import Store

TOKEN = "Bearer ghp_test"


class _Server(BaseHTTPRequestHandler):
    """A Streamable HTTP MCP server: wants the token, hands out a session,
    answers tools/call as an event stream and everything else as JSON."""

    seen: list[dict[str, Any]] = []
    deleted: list[str] = []

    def log_message(self, *args: Any) -> None:
        pass

    def _reply(self, code: int, body: bytes = b"", kind: str = "application/json", extra: dict[str, str] | None = None) -> None:
        self.send_response(code)
        if body:
            self.send_header("Content-Type", kind)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_DELETE(self) -> None:
        _Server.deleted.append(self.headers.get("Mcp-Session-Id", ""))
        self._reply(200)

    def do_POST(self) -> None:
        if self.path == "/moved":
            self._reply(307, extra={"Location": "http://example.invalid/steal"})
            return
        if self.headers.get("Authorization") != TOKEN:
            self._reply(401, b'{"error":"unauthorized"}', extra={"WWW-Authenticate": 'Bearer resource_metadata="x"'})
            return
        msg = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        _Server.seen.append({"msg": msg, "session": self.headers.get("Mcp-Session-Id"), "version": self.headers.get("MCP-Protocol-Version")})
        if "id" not in msg or "method" not in msg:
            self._reply(202)
            return
        if msg["method"] == "initialize":
            result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "fake", "version": "1"}}
            self._reply(200, json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result}).encode(), extra={"Mcp-Session-Id": "s-1"})
        elif msg["method"] == "tools/call":
            note = {"jsonrpc": "2.0", "method": "notifications/progress", "params": {"progress": 1}}
            answer = {"jsonrpc": "2.0", "id": msg["id"], "result": {"content": [{"type": "text", "text": "opened PR #7"}]}}
            body = f"event: message\ndata: {json.dumps(note)}\n\ndata: {json.dumps(answer)}\n\n".encode()
            self._reply(200, body, kind="text/event-stream")
        elif msg["method"] == "tools/list":
            tools = [{"name": "create_pull_request", "inputSchema": {"type": "object"}, "annotations": {"destructiveHint": False}}]
            self._reply(200, json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": {"tools": tools}}).encode())
        else:
            self._reply(200, json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": {}}).encode())


@pytest.fixture
def url() -> Iterator[str]:
    _Server.seen, _Server.deleted = [], []
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Server)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}/mcp"
    httpd.shutdown()
    httpd.server_close()


def _lines(*msgs: dict[str, Any]) -> io.BytesIO:
    return io.BytesIO(b"".join(json.dumps(m).encode() + b"\n" for m in msgs))


def _answers(out: io.BytesIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in out.getvalue().splitlines()]


INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "c", "version": "1"}},
}
CALL = {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "create_pull_request", "arguments": {"repo": "acme/shop"}}}


def test_the_bridge_carries_every_message_and_records_the_call(url: str, tmp_path: Path) -> None:
    store = Store(tmp_path / "g.db")
    out = io.BytesIO()
    stdin = _lines(INIT, {"jsonrpc": "2.0", "method": "notifications/initialized"}, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, CALL)
    code = _mcp_http.run(url, server="github", client="claude-code", store=store, headers={"Authorization": TOKEN}, stdin=stdin, stdout=out)
    assert code == 0
    answers = {a.get("id", a.get("method")): a for a in _answers(out)}
    assert answers[1]["result"]["serverInfo"]["name"] == "fake"
    assert answers[3]["result"]["content"][0]["text"] == "opened PR #7"
    assert "notifications/progress" in answers, "a message inside the event stream reaches the client too"
    later = [s for s in _Server.seen if s["msg"].get("method") != "initialize"]
    assert later and all(s["session"] == "s-1" and s["version"] == "2025-06-18" for s in later), "the session and protocol go on every later request"
    assert _Server.deleted == ["s-1"], "the session is ended when the client leaves"
    rows, _ = store.calls(0)
    assert [(r["server"], r["tool"], r["outcome"]) for r in rows] == [("github", "create_pull_request", "ok")]
    assert "opened PR #7" in rows[0]["result"]
    [srv] = [s for s in store.servers() if s["name"] == "github"] or [None]
    assert srv is None or [t["name"] for t in srv["tools"]] == ["create_pull_request"]


def test_a_refused_request_is_answered_so_the_client_does_not_wait(url: str) -> None:
    out = io.BytesIO()
    _mcp_http.run(url, server="github", client="c", store=None, headers={}, stdin=_lines(INIT), stdout=out)
    [answer] = _answers(out)
    assert answer["id"] == 1 and "HTTP 401" in answer["error"]["message"]


def test_a_redirect_is_not_followed_with_the_credentials(url: str) -> None:
    out = io.BytesIO()
    _mcp_http.run(url.replace("/mcp", "/moved"), server="x", client="c", store=None, headers={"Authorization": TOKEN}, stdin=_lines(INIT), stdout=out)
    [answer] = _answers(out)
    assert "HTTP 307" in answer["error"]["message"]


def test_headers_come_from_the_environment_with_variables_expanded() -> None:
    env = {_mcp_http.HEADERS_ENV: json.dumps({"Authorization": "Bearer ${GH_PAT}", "X-Team": "${TEAM:-core}"}), "GH_PAT": "ghp_1"}
    assert _mcp_http.headers_from_env(env) == {"Authorization": "Bearer ghp_1", "X-Team": "core"}


def test_probe(url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.undo()  # the real probe, against the local server
    assert _mcp_http.probe(url, {"Authorization": TOKEN}) == "ok"
    assert _Server.deleted == ["s-1"], "the probe ends the session it opened"
    assert _mcp_http.probe(url, {}) == "sign-in"
    assert _mcp_http.probe("http://127.0.0.1:9/mcp", {}) == "unreachable"


def _entry(url: str) -> dict[str, Any]:
    return {"type": "http", "url": url, "headers": {"Authorization": "Bearer ${GH_PAT}"}}


def test_gateway_on_wraps_an_http_server_and_off_puts_it_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    probed: list[tuple[str, dict[str, str]]] = []
    monkeypatch.setattr(_mcp_http, "probe", lambda u, h: probed.append((u, h)) or "ok")
    monkeypatch.setenv("GH_PAT", "ghp_1")
    doc = {"mcpServers": {"github": _entry("https://api.githubcopilot.com/mcp/"), "old": {"type": "sse", "url": "https://x.example/sse"}}}
    path = tmp_path / ".claude.json"
    path.write_text(json.dumps(doc))
    files = [ClientFile("claude-code", path)]

    found = {f.name: f for f in _gateway.on(files)}
    assert found["github"].wrapped and found["github"].transport == "http" and found["github"].target == "https://api.githubcopilot.com/mcp/"
    assert probed == [("https://api.githubcopilot.com/mcp/", {"Authorization": "Bearer ghp_1"})], "probed with the variables expanded"
    assert not found["old"].wrapped and "sse" in (found["old"].why or "")
    gh = json.loads(path.read_text())["mcpServers"]["github"]
    assert gh["type"] == "stdio" and gh["args"][-2:] == ["-url", "https://api.githubcopilot.com/mcp/"]
    assert json.loads(gh["env"][_mcp_http.HEADERS_ENV]) == {"Authorization": "Bearer ${GH_PAT}"}, "the client expands the variable, as before"
    assert gh["toolpass"] == _entry("https://api.githubcopilot.com/mcp/")

    _gateway.off(files)
    assert json.loads(path.read_text()) == doc


def test_a_server_that_signs_in_with_oauth_is_left_alone(tmp_path: Path) -> None:
    path = tmp_path / ".claude.json"
    doc = {"mcpServers": {"linear": {"type": "http", "url": "https://mcp.linear.app/mcp"}}}
    path.write_text(json.dumps(doc))
    [f] = _gateway.on([ClientFile("claude-code", path)])
    assert not f.wrapped and "OAuth" in (f.why or "")
    assert json.loads(path.read_text()) == doc

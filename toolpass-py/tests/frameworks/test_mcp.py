"""Secured tools in a real MCP server, with a real MCP client.

The server is the official ``mcp`` SDK's ``MCPServer``; the tools are plain
functions under the toolkit's decorator, so the server needs no adapter.
Transports: in-memory (``Client(server)``), stdio (the example server as a
subprocess, the user from ``AGENT_USER``) and Streamable HTTP (uvicorn on a
local port), where ``session_from_header`` takes the user from the
``X-Toolpass-User`` header the agent sends. The client is driven by a
scripted loop that makes the tool calls a model would; toolpass is a real
in-process engine, and the wire scenario's fake GitHub decides what reached
it.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
import sys
import threading
import time
from collections.abc import Iterator, Sequence
from typing import Any

import pytest

pytest.importorskip("mcp.server.mcpserver")

from mcp import Client
from mcp.client.stdio import StdioServerParameters
from mcp.client.streamable_http import streamable_http_client
from mcp.server.mcpserver import MCPServer
from mcp_types import CallToolResult

from tests import harness
from tests.frameworks.wire_scenario import ADMIN, DANA, REFUSAL_WORDS, REPO, propose_body, requests_seen, wire_engine
from toolpass import Session, Toolkit, session_from_header

EXAMPLES = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "examples")
KEY = "agent-key-1"


def text(res: CallToolResult) -> str:
    [block] = res.content
    return str(block.text)  # type: ignore[union-attr]


async def drive(client: Client, calls: Sequence[tuple[str, dict[str, Any]]]) -> list[CallToolResult]:
    """The scripted model: make each call over MCP and hand back what the client got."""
    return [await client.call_tool(name, args) for name, args in calls]


def run(server: Any, calls: Sequence[tuple[str, dict[str, Any]]]) -> list[CallToolResult]:
    async def go() -> list[CallToolResult]:
        async with Client(server) as client:
            return await drive(client, calls)

    return asyncio.run(go())


def wire_server(gh: harness.Server) -> tuple[MCPServer[Any], Any]:
    """The wire scenario's secured tool, registered as an MCP tool. The SDK
    reports an unexpected exception as only ``Error executing tool <name>``,
    so ``on_refuse=str`` hands the refusal to the model as the tool's result."""
    engine = wire_engine(gh)
    tools = Toolkit(audit=None, permissions=engine, on_refuse=str)
    server: MCPServer[Any] = MCPServer("fw-mcp-wire")

    @server.tool()
    @tools.tool()
    def propose_fix(repo: str) -> str:
        """Open a pull request fixing a file."""
        return propose_body(gh, repo)

    return server, engine


# -- in-memory ------------------------------------------------------------------------


def test_wire_hook_through_the_server(wire_gh: harness.Server) -> None:
    """No rule and no guard: a ``@tools.tool()`` body registered as an MCP tool
    has its own HTTP requests mapped to actions and checked against the
    session's user at the wire. The MCP SDK runs a sync tool on a worker thread
    under a copy of the handler's context, so the session made current around
    the client call reaches the hook."""
    gh = wire_gh
    server, engine = wire_server(gh)

    with Session(DANA).active():
        [refused] = run(server, [("propose_fix", {"repo": REPO})])
    for word in REFUSAL_WORDS:
        assert word in text(refused), text(refused)
    assert requests_seen(gh) == ["GET /api/v3/repos/acme/x"], "the POST never left the process"
    assert [a[2] for a in engine.asked] == ["repo.read", "pr.create"]

    with Session(ADMIN).active():
        [opened] = run(server, [("propose_fix", {"repo": REPO})])
    assert (opened.is_error, text(opened)) == (False, f"opened #7 in {REPO}")
    assert requests_seen(gh) == ["GET /api/v3/repos/acme/x", "GET /api/v3/repos/acme/x", "POST /api/v3/repos/acme/x/pulls"]


def test_no_session_refuses(wire_gh: harness.Server) -> None:
    server, _ = wire_server(wire_gh)
    [refused] = run(server, [("propose_fix", {"repo": REPO})])
    assert "no_session" in text(refused), text(refused)
    assert requests_seen(wire_gh) == []


# -- stdio: the example server, one process per user ------------------------------------------


def test_stdio_example_server() -> None:
    async def go(user: str) -> list[CallToolResult]:
        params = StdioServerParameters(
            command=sys.executable,
            args=[os.path.join(EXAMPLES, "mcp_server.py")],
            env={"AGENT_USER": user, "PYTHONPATH": os.pathsep.join(p for p in sys.path if p)},
        )
        async with Client(params) as client:
            return await drive(client, [("write_thing", {"thing_id": "1", "content": "hi"}), ("read_thing", {"thing_id": "1"})])

    admin_write, admin_read = asyncio.run(go(ADMIN))
    assert (admin_write.is_error, text(admin_write)) == (False, "wrote 2 bytes to thing:1")
    assert (admin_read.is_error, text(admin_read)) == (False, "thing:1 says hello")
    dana_write, dana_read = asyncio.run(go(DANA))
    assert text(dana_write) == (
        "write_thing refused (not_authorized): dana@example.com may not run write_thing: "
        "toolpass deny thing.write on thing:1 in demo: denied: dana@example.com is not an admin"
    )
    assert (dana_read.is_error, text(dana_read)) == (False, "thing:1 says hello")


# -- Streamable HTTP: the user from the X-Toolpass-User header ---------------------------------


@contextlib.contextmanager
def _http_server(app: Any) -> Iterator[str]:
    uvicorn = pytest.importorskip("uvicorn")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    deadline = time.monotonic() + 10
    while not srv.started:
        assert time.monotonic() < deadline, "uvicorn did not start"
        time.sleep(0.02)
    try:
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        srv.should_exit = True
        t.join(5)


def call_http(url: str, headers: dict[str, str], calls: Sequence[tuple[str, dict[str, Any]]]) -> list[CallToolResult]:
    import httpx2

    async def go() -> list[CallToolResult]:
        async with httpx2.AsyncClient(headers=headers) as http:
            async with Client(streamable_http_client(url, http_client=http)) as client:
                return await drive(client, calls)

    return asyncio.run(go())


def post_raw(url: str, headers: dict[str, str]) -> tuple[int, str]:
    import httpx2

    r = httpx2.post(url, headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    return r.status_code, r.text


def test_http_user_from_header(wire_gh: harness.Server) -> None:
    """The header names the user; the body's requests are checked against that
    user at the wire, in the SDK's own request handling under uvicorn."""
    gh = wire_gh
    server, engine = wire_server(gh)
    with _http_server(session_from_header(server.streamable_http_app())) as url:
        [refused] = call_http(url, {"X-Toolpass-User": DANA}, [("propose_fix", {"repo": REPO})])
        for word in REFUSAL_WORDS:
            assert word in text(refused), text(refused)
        assert requests_seen(gh) == ["GET /api/v3/repos/acme/x"], "the POST never left the process"
        [opened] = call_http(url, {"X-Toolpass-User": ADMIN}, [("propose_fix", {"repo": REPO})])
        assert (opened.is_error, text(opened)) == (False, f"opened #7 in {REPO}")
        assert [(a[0], a[2]) for a in engine.asked] == [(DANA, "repo.read"), (DANA, "pr.create"), (ADMIN, "repo.read"), (ADMIN, "pr.create")]


def test_http_one_session_per_conversation(wire_gh: harness.Server) -> None:
    """The requests of one conversation, named by the agent in
    X-Toolpass-Session, share one Session, so a tool's ``limit`` holds across
    its calls; another conversation starts afresh. (This SDK's client speaks
    the sessionless protocol, so there is no Mcp-Session-Id to go by.)"""
    tools = Toolkit(audit=None, on_refuse=str)
    server: MCPServer[Any] = MCPServer("fw-mcp-limit")

    @server.tool()
    @tools.tool(effect="write", limit=1)
    def bump(n: int) -> str:
        """Bump a counter."""
        return f"bumped {n}"

    with _http_server(session_from_header(server.streamable_http_app())) as url:
        chat = {"X-Toolpass-User": DANA, "X-Toolpass-Session": "chat-1"}
        first, second = call_http(url, chat, [("bump", {"n": 1}), ("bump", {"n": 2})])
        assert (first.is_error, text(first)) == (False, "bumped 1")
        assert "limit_reached" in text(second), text(second)
        [third] = call_http(url, chat, [("bump", {"n": 3})])
        assert "limit_reached" in text(third), "a new MCP client in the same conversation: the same session"
        [again] = call_http(url, {"X-Toolpass-User": DANA, "X-Toolpass-Session": "chat-2"}, [("bump", {"n": 4})])
        assert (again.is_error, text(again)) == (False, "bumped 4")
        [alone] = call_http(url, {"X-Toolpass-User": DANA}, [("bump", {"n": 5})])
        assert (alone.is_error, text(alone)) == (False, "bumped 5"), "no conversation named: a session of its own"


def test_http_without_header_has_no_session(wire_gh: harness.Server) -> None:
    """A request without the header runs under no session: the secured tool
    refuses, and a session active in the server's process does not leak in."""
    server, _ = wire_server(wire_gh)
    with Session(ADMIN).active(), _http_server(session_from_header(server.streamable_http_app())) as url:
        [refused] = call_http(url, {}, [("propose_fix", {"repo": REPO})])
    assert "no_session" in text(refused), text(refused)
    assert requests_seen(wire_gh) == []


def test_http_bad_user_headers_are_refused(wire_gh: harness.Server) -> None:
    server, _ = wire_server(wire_gh)
    with _http_server(session_from_header(server.streamable_http_app())) as url:
        status, body = post_raw(url, {"X-Toolpass-User": ""})
        assert (status, body) == (400, "toolpass: the X-Toolpass-User header is empty")
        import httpx2

        r = httpx2.post(url, headers=httpx2.Headers([("X-Toolpass-User", DANA), ("X-Toolpass-User", ADMIN)]), json={})
        assert (r.status_code, r.text) == (400, "toolpass: the request carries more than one X-Toolpass-User header")
    assert requests_seen(wire_gh) == []


def test_http_key(wire_gh: harness.Server) -> None:
    """With ``key``, only a request carrying it in X-Toolpass-Key gets in; a
    missing, wrong or doubled key is refused before the server sees it."""
    server, _ = wire_server(wire_gh)
    with _http_server(session_from_header(server.streamable_http_app(), key=KEY)) as url:
        for headers in ({}, {"X-Toolpass-Key": "wrong"}, {"X-Toolpass-User": ADMIN}):
            status, body = post_raw(url, headers)
            assert status == 403, (headers, status, body)
            assert body == "toolpass: this server needs the X-Toolpass-Key header, and this request's is missing or wrong"
        [opened] = call_http(url, {"X-Toolpass-User": ADMIN, "X-Toolpass-Key": KEY}, [("propose_fix", {"repo": REPO})])
        assert (opened.is_error, text(opened)) == (False, f"opened #7 in {REPO}")

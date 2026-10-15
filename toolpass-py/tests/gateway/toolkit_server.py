"""A stdio MCP server whose tool is a toolpass secured tool calling a fake
GitHub, for the gateway's second-layer tests. The user comes from
DEMO_USER; the fake's URL from FAKE_GH. Exits when its stdin closes."""

from __future__ import annotations

import json
import os
import sys

from tests.harness.wire import Engine, http
from toolpass import Session, Toolkit, ToolRefused, as_agent

GH = os.environ["FAKE_GH"]
engine = Engine(GH)
engine.allow[("admin@example.com", "pr.create")] = True
tools = Toolkit(permissions=engine, audit=None)


@tools.tool()
def open_pr(repo: str, title: str) -> str:
    with as_agent():
        http("GET", f"{GH}/api/v3/repos/{repo}")
    pr = http("POST", f"{GH}/api/v3/repos/{repo}/pulls", {"title": title, "head": "fix", "base": "main"})
    return f"opened #{pr['number']}"


def answer(msg: dict) -> dict | None:
    if msg.get("id") is None:
        return None
    method = msg.get("method")
    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": msg["id"],
            "result": {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "t", "version": "1"}},
        }
    if method == "tools/call":
        args = msg["params"].get("arguments", {})
        try:
            with Session(os.environ["DEMO_USER"]).active():
                text, error = open_pr(**args), False
        except ToolRefused as e:
            text, error = f"{e.code}: {e.reason}", True
        return {"jsonrpc": "2.0", "id": msg["id"], "result": {"content": [{"type": "text", "text": text}], "isError": error}}
    return {"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32601, "message": "method not found"}}


for line in sys.stdin:
    if line.strip():
        out = answer(json.loads(line))
        if out is not None:
            sys.stdout.write(json.dumps(out) + "\n")
            sys.stdout.flush()

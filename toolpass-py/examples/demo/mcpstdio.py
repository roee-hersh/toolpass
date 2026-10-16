"""Just enough of the MCP stdio protocol to serve a few Python functions as
tools, so the demo needs nothing but toolpass. A real server would use the
official ``mcp`` SDK; the tools would not change.

Each function becomes a tool named after it; its docstring is the
description and its ``str`` parameters the input schema. A function that
raises returns its message as an error result, as MCP servers do."""

from __future__ import annotations

import inspect
import json
import sys
from collections.abc import Callable
from typing import IO, Any

Tool = Callable[..., str]


def _schema(fn: Tool) -> dict[str, Any]:
    params = inspect.signature(fn).parameters
    return {
        "type": "object",
        "properties": {name: {"type": "string"} for name in params},
        "required": [name for name, p in params.items() if p.default is inspect.Parameter.empty],
    }


def _describe(fn: Tool, annotations: dict[str, Any]) -> dict[str, Any]:
    return {"name": fn.__name__, "description": inspect.getdoc(fn) or "", "inputSchema": _schema(fn), "annotations": annotations}


def _answer(
    msg: dict[str, Any], tools: dict[str, tuple[Tool, dict[str, Any]]], name: str, call: Callable[[Tool, dict[str, Any]], str]
) -> dict[str, Any] | None:
    if msg.get("id") is None:
        return None  # a notification
    method, rid = msg.get("method"), msg["id"]
    if method == "initialize":
        info = {
            "protocolVersion": msg.get("params", {}).get("protocolVersion", "2025-06-18"),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": name, "version": "1"},
        }
        return {"jsonrpc": "2.0", "id": rid, "result": info}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": rid, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"tools": [_describe(fn, ann) for fn, ann in tools.values()]}}
    if method == "tools/call":
        params = msg.get("params") or {}
        entry = tools.get(params.get("name", ""))
        if entry is None:
            return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32602, "message": f"unknown tool {params.get('name')!r}"}}
        try:
            text, error = call(entry[0], dict(params.get("arguments") or {})), False
        except Exception as e:  # noqa: BLE001 - every failure is the tool's answer to the model
            text, error = str(e), True
        return {"jsonrpc": "2.0", "id": rid, "result": {"content": [{"type": "text", "text": text}], "isError": error}}
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"method {method!r} not found"}}


def serve(
    name: str,
    tools: list[tuple[Tool, dict[str, Any]]],
    call: Callable[[Tool, dict[str, Any]], str] = lambda fn, args: fn(**args),
    stdin: IO[str] = sys.stdin,
    stdout: IO[str] = sys.stdout,
) -> None:
    """Answer MCP requests on stdin until it closes. ``tools`` pairs each
    function with its MCP annotations (``readOnlyHint``, ``destructiveHint``);
    ``call`` runs one, so the caller can set up the session around it."""
    by_name = {fn.__name__: (fn, ann) for fn, ann in tools}
    for line in stdin:
        if not line.strip():
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        out = _answer(msg, by_name, name, call) if isinstance(msg, dict) else None
        if out is not None:
            stdout.write(json.dumps(out) + "\n")
            stdout.flush()

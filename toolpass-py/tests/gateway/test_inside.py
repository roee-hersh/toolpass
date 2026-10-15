"""The second layer: what a toolpass-secured tool did inside an MCP call
(each request to GitHub, allowed or refused for the user) is recorded under
that call."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from tests import harness
from tests.harness.wire import requests_seen
from toolpass._store import Store

ROOT = Path(__file__).resolve().parents[2]
SERVER = str(Path(__file__).with_name("toolkit_server.py"))


def _run(tmp_path: Path, gh: harness.Server, user: str) -> bytes:
    env = {**os.environ, "TOOLPASS_DATA_DIR": str(tmp_path), "FAKE_GH": gh.url, "DEMO_USER": user, "PYTHONPATH": str(ROOT)}
    msgs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "open_pr", "arguments": {"repo": "acme/x", "title": "fix"}}},
    ]
    data = b"".join(json.dumps(m).encode() + b"\n" for m in msgs)
    cmd = [sys.executable, "-m", "toolpass.cli", "mcp", "-server", "repos", "-client", "test", "--", sys.executable, SERVER]
    done = subprocess.run(cmd, input=data, capture_output=True, env=env, timeout=60, cwd=str(ROOT))
    assert done.returncode == 0, done.stderr.decode(errors="replace")
    return done.stdout


def _call(tmp_path: Path) -> dict:
    store = Store(tmp_path / "gateway.db")
    for _ in range(50):  # the server's writer finishes within its exit; give it a moment on a slow runner
        rows, _ = store.calls(0)
        if rows and rows[-1]["inside"]:
            return rows[-1]
        time.sleep(0.1)
    return store.calls(0)[0][-1]


def test_an_allowed_call_shows_each_request_under_it(tmp_path: Path) -> None:
    with harness.Server(tls=False) as gh:
        gh.json("GET", "/api/v3/repos/acme/x", 200, {"full_name": "acme/x"})
        gh.json("POST", "/api/v3/repos/acme/x/pulls", 201, {"number": 7})
        out = _run(tmp_path, gh, "admin@example.com")
        assert b"opened #7" in out
        assert requests_seen(gh) == ["GET /api/v3/repos/acme/x", "POST /api/v3/repos/acme/x/pulls"]
    call = _call(tmp_path)
    assert call["tool"] == "open_pr" and call["outcome"] == "ok"
    [event] = call["inside"]
    assert event["tool"] == "open_pr" and event["user"] == "admin@example.com" and event["outcome"] == "ran"
    acts = [(a["method"], a.get("action"), a.get("resource"), a["outcome"]) for a in event["actions"]]
    assert acts == [("GET", "github:repo.read", "repo:acme/x", "allowed"), ("POST", "github:pr.create", "repo:acme/x", "allowed")]
    assert event["actions"][0].get("as_agent") is True


def test_a_refused_request_is_shown_and_never_sent(tmp_path: Path) -> None:
    with harness.Server(tls=False) as gh:
        gh.json("GET", "/api/v3/repos/acme/x", 200, {"full_name": "acme/x"})
        gh.json("POST", "/api/v3/repos/acme/x/pulls", 201, {"number": 7})
        out = _run(tmp_path, gh, "dana@example.com")
        assert b"not_authorized" in out
        assert requests_seen(gh) == ["GET /api/v3/repos/acme/x"], "the pull request never reached GitHub"
    call = _call(tmp_path)
    assert call["outcome"] == "error"
    [event] = call["inside"]
    assert event["user"] == "dana@example.com" and event["outcome"] == "refused" and event["code"] == "not_authorized"
    refused = [a for a in event["actions"] if a["outcome"] == "refused"]
    assert [(a["action"], a["resource"]) for a in refused] == [("github:pr.create", "repo:acme/x")]


def test_tool_events_outside_any_call_are_not_attached(tmp_path: Path) -> None:
    store = Store(tmp_path / "g.db")
    call = store.start_call("c", "cfg", "srv", "sess", "open_pr", {})
    store.finish_call(call, "ok", "done", 1.0)
    now = time.time()
    store.record_tool_event("sess", {"tool": "late"}, now + 60, now + 61)  # long after the call ended
    store.record_tool_event("other", {"tool": "elsewhere"}, now, now)  # another session
    assert store.calls(0)[0][0]["inside"] == []


def test_an_event_landing_after_the_call_finished_moves_its_rev(tmp_path: Path) -> None:
    store = Store(tmp_path / "g.db")
    call = store.start_call("c", "cfg", "srv", "sess", "open_pr", {})
    store.finish_call(call, "ok", "done", 1.0)
    _, nxt = store.calls(0)
    store.record_tool_event("sess", {"tool": "open_pr", "outcome": "ran"}, time.time() - 0.5, time.time() - 0.4)
    rows, _ = store.calls(nxt)
    assert [r["id"] for r in rows] == [call] and rows[0]["inside"][0]["tool"] == "open_pr"

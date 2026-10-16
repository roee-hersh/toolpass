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
    t = time.time() - 10
    call = store.start_call("c", "cfg", "srv", "sess", "open_pr", {}, started=t)
    store.finish_call(call, "ok", "done", 1000.0, ended=t + 1)
    _, nxt = store.calls(0)
    store.record_tool_event("sess", {"tool": "open_pr", "outcome": "ran"}, t + 0.1, t + 0.9)
    rows, _ = store.calls(nxt)
    assert [r["id"] for r in rows] == [call] and rows[0]["inside"][0]["tool"] == "open_pr"


def test_back_to_back_calls_each_get_only_their_own_events(tmp_path: Path) -> None:
    # Calls a few milliseconds apart: each event goes to the call it ran in, never to its neighbours.
    store = Store(tmp_path / "g.db")
    t = time.time() - 10
    ids = []
    for i in range(3):
        start = t + i * 0.004
        ids.append(store.start_call("c", "cfg", "srv", "sess", "ship", {"n": i}, started=start))
        store.record_tool_event("sess", {"tool": "ship", "n": i}, start + 0.0005, start + 0.003)
        store.finish_call(ids[-1], "ok", "done", 3.5, ended=start + 0.0035)
    rows = {r["id"]: r for r in store.calls(0)[0]}
    assert [[e["n"] for e in rows[i]["inside"]] for i in ids] == [[0], [1], [2]]


def test_two_tools_running_at_once_are_told_apart_by_name(tmp_path: Path) -> None:
    store = Store(tmp_path / "g.db")
    t = time.time() - 10
    a = store.start_call("c", "cfg", "srv", "sess", "status", {}, started=t)
    b = store.start_call("c", "cfg", "srv", "sess", "ship", {}, started=t + 0.01)
    store.record_tool_event("sess", {"tool": "status"}, t + 0.02, t + 0.5)
    store.record_tool_event("sess", {"tool": "ship"}, t + 0.03, t + 0.4)
    store.finish_call(a, "ok", "", 600.0, ended=t + 0.6)
    store.finish_call(b, "ok", "", 500.0, ended=t + 0.5)
    rows = {r["id"]: r for r in store.calls(0)[0]}
    assert [e["tool"] for e in rows[a]["inside"]] == ["status"]
    assert [e["tool"] for e in rows[b]["inside"]] == ["ship"]


def test_two_calls_of_one_tool_at_once_pair_up_in_the_order_they_were_sent(tmp_path: Path) -> None:
    # Parallel tool use: A and B sent 1 ms apart; each run starts after both were sent.
    store = Store(tmp_path / "g.db")
    t = time.time() - 10
    a = store.start_call("c", "cfg", "srv", "sess", "open_pr", {"n": "a"}, started=t)
    b = store.start_call("c", "cfg", "srv", "sess", "open_pr", {"n": "b"}, started=t + 0.001)
    store.record_tool_event("sess", {"tool": "open_pr", "n": "a"}, t + 0.002, t + 0.2)
    store.record_tool_event("sess", {"tool": "open_pr", "n": "b"}, t + 0.003, t + 0.3)
    store.finish_call(a, "ok", "", 210.0, ended=t + 0.21)
    store.finish_call(b, "ok", "", 310.0, ended=t + 0.31)
    rows = {r["id"]: r for r in store.calls(0)[0]}
    assert [e["n"] for e in rows[a]["inside"]] == ["a"]
    assert [e["n"] for e in rows[b]["inside"]] == ["b"]


def test_a_tool_named_differently_by_the_server_lands_on_the_call_running_then(tmp_path: Path) -> None:
    store = Store(tmp_path / "g.db")
    t = time.time() - 10
    call = store.start_call("c", "cfg", "srv", "sess", "github_open_pr", {}, started=t)
    store.record_tool_event("sess", {"tool": "open_pr"}, t + 0.01, t + 0.1)
    store.finish_call(call, "ok", "", 110.0, ended=t + 0.11)
    assert [e["tool"] for e in store.calls(0)[0][0]["inside"]] == ["open_pr"]


def test_an_event_recorded_before_its_call_is_placed_when_the_call_is(tmp_path: Path) -> None:
    store = Store(tmp_path / "g.db")
    t = time.time() - 10
    store.record_tool_event("sess", {"tool": "open_pr"}, t + 0.01, t + 0.1)  # the proxy's writer was slower
    call = store.start_call("c", "cfg", "srv", "sess", "open_pr", {}, started=t)
    store.finish_call(call, "ok", "", 110.0, ended=t + 0.11)
    assert [e["tool"] for e in store.calls(0)[0][0]["inside"]] == ["open_pr"]


def test_two_servers_upgrading_one_old_record_at_once(tmp_path: Path) -> None:
    # Both read the old schema; the second adds a column the first already added.
    import sqlite3

    from toolpass._store import _add_column

    db = sqlite3.connect(tmp_path / "g.db")
    db.execute("CREATE TABLE calls (id INTEGER PRIMARY KEY)")
    _add_column(db, "calls", "ended", "REAL")
    _add_column(db, "calls", "ended", "REAL")
    assert [r[1] for r in db.execute("PRAGMA table_info(calls)").fetchall()] == ["id", "ended"]


def test_runs_recorded_before_their_calls_when_the_proxy_writer_lags(tmp_path: Path) -> None:
    # The server's runs reach the record before any of the three calls does (a slow disk).
    store = Store(tmp_path / "g.db")
    t = time.time() - 10
    for i, tool in enumerate(("status", "open_pr", "status")):
        store.record_tool_event("sess", {"tool": tool, "n": i}, t + i + 0.01, t + i + 0.5)
    ids = []
    for i, tool in enumerate(("status", "open_pr", "status")):
        ids.append(store.start_call("c", "cfg", "srv", "sess", tool, {}, started=t + i))
    for i, call in enumerate(ids):
        store.finish_call(call, "ok", "", 600.0, ended=t + i + 0.6)
    rows = {r["id"]: r for r in store.calls(0)[0]}
    assert [[e["n"] for e in rows[c]["inside"]] for c in ids] == [[0], [1], [2]]


def test_a_call_not_finished_yet_does_not_take_the_next_calls_run(tmp_path: Path) -> None:
    # Call 1's answer is written late; call 2's run arrives in between.
    store = Store(tmp_path / "g.db")
    t = time.time() - 10
    one = store.start_call("c", "cfg", "srv", "sess", "ship", {}, started=t)
    store.record_tool_event("sess", {"tool": "ship", "n": 1}, t + 0.01, t + 0.2)
    two = store.start_call("c", "cfg", "srv", "sess", "ship", {}, started=t + 0.3)
    store.record_tool_event("sess", {"tool": "ship", "n": 2}, t + 0.31, t + 0.5)
    store.finish_call(one, "ok", "", 250.0, ended=t + 0.25)
    store.finish_call(two, "ok", "", 250.0, ended=t + 0.55)
    rows = {r["id"]: r for r in store.calls(0)[0]}
    assert [e["n"] for e in rows[one]["inside"]] == [1]
    assert [e["n"] for e in rows[two]["inside"]] == [2]


def test_a_coarse_wall_clock_does_not_mix_up_two_quick_calls(tmp_path: Path) -> None:
    # Windows' wall clock on Python 3.10 ticks every 15.6 ms: two short calls of one tool share
    # their wall stamps, and the second's run arrives before its call is recorded. The precise
    # clock (t0/t1) still tells them apart.
    store = Store(tmp_path / "g.db")
    wall = time.time() - 10
    one = store.start_call("c", "cfg", "srv", "sess", "ship", {"n": 1}, started=wall, t0=100.000)
    store.record_tool_event("sess", {"tool": "ship", "n": 1}, wall, wall, t0=100.001, t1=100.090)
    store.finish_call(one, "ok", "", 95.0, ended=wall, t1=100.095)
    store.record_tool_event("sess", {"tool": "ship", "n": 2}, wall, wall, t0=100.097, t1=100.100)  # before call 2 is recorded
    two = store.start_call("c", "cfg", "srv", "sess", "ship", {"n": 2}, started=wall, t0=100.096)
    store.finish_call(two, "error", "", 5.0, ended=wall, t1=100.101)
    rows = {r["id"]: r for r in store.calls(0)[0]}
    assert [e["n"] for e in rows[one]["inside"]] == [1]
    assert [e["n"] for e in rows[two]["inside"]] == [2]


def test_a_record_from_before_the_precise_clock_is_upgraded(tmp_path: Path) -> None:
    import sqlite3

    path = tmp_path / "g.db"
    Store(path).close()
    db = sqlite3.connect(path)
    db.executescript(
        "DROP TABLE tool_events; CREATE TABLE tool_events (id INTEGER PRIMARY KEY AUTOINCREMENT, session TEXT NOT NULL, tool TEXT NOT NULL, started REAL NOT NULL, ended REAL NOT NULL, call_id INTEGER, event TEXT NOT NULL);"
    )
    db.close()
    store = Store(path)  # adds t0/t1
    call = store.start_call("c", "cfg", "srv", "s", "ship", {}, t0=1.0)
    store.record_tool_event("s", {"tool": "ship"}, time.time(), time.time(), t0=1.001, t1=1.002)
    store.finish_call(call, "ok", "", 3.0, t1=1.003)
    assert [e["tool"] for e in store.calls(0)[0][0]["inside"]] == ["ship"]

"""`toolpass mcp`: every byte goes through unchanged, and every tool call is recorded."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from toolpass._mcp_proxy import Recorder
from toolpass._store import Store

FAKE = str(Path(__file__).with_name("fake_server.py"))

SESSION = [
    {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}},
    },
    {"jsonrpc": "2.0", "method": "notifications/initialized"},
    {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "echo", "arguments": {"text": "hello é"}}},
    {"jsonrpc": "2.0", "id": "x4", "method": "tools/call", "params": {"name": "delete_repo", "arguments": {"repo": "acme/x"}}},
    {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "fail", "arguments": {}}},
    {"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {"name": "nope", "arguments": {}}},
]


def _talk(cmd: list[str], env: dict[str, str]) -> bytes:
    data = b"".join(json.dumps(m).encode() + b"\n" for m in SESSION)
    done = subprocess.run(cmd, input=data, capture_output=True, env=env, timeout=60)
    assert done.returncode == 0, done.stderr.decode(errors="replace")
    return done.stdout


def test_bytes_pass_unchanged_and_calls_are_recorded(tmp_path: Path) -> None:
    env = {**os.environ, "TOOLPASS_DATA_DIR": str(tmp_path)}
    direct = _talk([sys.executable, FAKE], env)
    proxied = _talk([sys.executable, "-m", "toolpass.cli", "mcp", "-server", "fake", "-client", "test", "--", sys.executable, FAKE], env)
    assert proxied == direct, "the client sees exactly what the server sent"

    store = Store(tmp_path / "gateway.db")
    rows, _ = store.calls(0)
    by_tool = {r["tool"]: r for r in rows}
    assert set(by_tool) == {"echo", "delete_repo", "fail", "nope"}
    assert by_tool["echo"]["outcome"] == "ok" and by_tool["echo"]["result"] == "hello é"
    assert by_tool["echo"]["arguments"] == {"text": "hello é"}
    assert by_tool["delete_repo"]["outcome"] == "ok" and by_tool["delete_repo"]["result"] == "deleted acme/x"
    assert by_tool["fail"]["outcome"] == "error" and by_tool["fail"]["result"] == "it failed"
    assert by_tool["nope"]["outcome"] == "error" and "unknown tool" in by_tool["nope"]["result"]
    assert all(r["client"] == "test" and r["server"] == "fake" and r["duration_ms"] is not None for r in rows)
    assert len({r["session"] for r in rows}) == 1
    store.record_server("test", "fake", "", "stdio", "fake", True)
    tools = {t["name"]: t["annotations"] for t in store.servers()[0]["tools"]}
    assert tools["delete_repo"] == {"destructiveHint": True} and tools["echo"] == {"readOnlyHint": True}


def test_a_server_that_cannot_start(tmp_path: Path) -> None:
    env = {**os.environ, "TOOLPASS_DATA_DIR": str(tmp_path)}
    done = subprocess.run(
        [sys.executable, "-m", "toolpass.cli", "mcp", "-server", "x", "--", str(tmp_path / "no-such-program")], capture_output=True, env=env, timeout=60
    )
    assert done.returncode == 127 and b"could not start" in done.stderr and done.stdout == b""


def test_missing_command(tmp_path: Path) -> None:
    env = {**os.environ, "TOOLPASS_DATA_DIR": str(tmp_path)}
    done = subprocess.run([sys.executable, "-m", "toolpass.cli", "mcp", "-server", "x"], capture_output=True, env=env, timeout=60)
    assert done.returncode == 2 and b"missing the server command" in done.stderr


class Broken:
    def __getattr__(self, name: str) -> Any:
        def fail(*a: Any, **k: Any) -> Any:
            raise RuntimeError("disk full")

        return fail


def _client(r: Recorder, line: bytes) -> None:
    r.note_client(line)


def test_recorder_survives_garbage_and_a_broken_store(capsys: pytest.CaptureFixture[str]) -> None:
    r = Recorder(Broken(), "c", "s")  # type: ignore[arg-type]
    for line in [b"not json\n", b"[1, 2]\n", b'{"id": true, "method": "tools/call"}\n', b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":"x"}\n']:
        _client(r, line)
    r.from_server(b'{"jsonrpc":"2.0","id":1,"result":{}}\n')
    r.close()
    err = capsys.readouterr().err
    assert err.count("could not record") == 1, "one warning, not one per call"


def test_recorder_batches_and_server_requests(tmp_path: Path) -> None:
    store = Store(tmp_path / "g.db")
    r = Recorder(store, "c", "s")
    _client(r, b'[{"jsonrpc":"2.0","id":7,"method":"tools/call","params":{"name":"a","arguments":{}}},{"jsonrpc":"2.0","id":8,"method":"ping"}]\n')
    # a request from the server that reuses id 7 is not mistaken for the response
    r.from_server(b'{"jsonrpc":"2.0","id":7,"method":"sampling/createMessage","params":{}}\n')
    r.from_server(b'[{"jsonrpc":"2.0","id":7,"result":{"content":[{"type":"image","data":"..."}]}}]\n')
    r.close()
    row = store.calls(0)[0][0]
    assert row["outcome"] == "ok" and row["result"] == "[image]"


def test_long_arguments_and_results_are_shortened(tmp_path: Path) -> None:
    store = Store(tmp_path / "g.db")
    r = Recorder(store, "c", "s")
    _client(r, json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "a", "arguments": {"t": "x" * 5000}}}).encode())
    r.from_server(json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": "y" * 9000}]}}).encode())
    r.close()
    row = store.calls(0)[0][0]
    assert len(row["arguments"]["t"]) < 300 and len(row["result"]) < 4100


def test_an_answer_faster_than_the_record_still_finds_its_call(tmp_path: Path) -> None:
    """The server may answer before the call's row is written: the writer
    keeps the order, so the result lands on its call."""
    store = Store(tmp_path / "g.db")
    r = Recorder(store, "c", "s", "cfg")
    r.note_client(b'{"jsonrpc":"2.0","id":9,"method":"tools/call","params":{"name":"a","arguments":{}}}\n')
    r.from_server(b'{"jsonrpc":"2.0","id":9,"result":{"content":[{"type":"text","text":"fast"}]}}\n')
    r.close()
    row = store.calls(0)[0][0]
    assert row["outcome"] == "ok" and row["result"] == "fast" and row["config"] == "cfg"


class Slow:
    """A store whose every write takes a while: the pump must not wait for it."""

    def __init__(self, inner: Store) -> None:
        self.inner = inner

    def __getattr__(self, name: str) -> Any:
        fn = getattr(self.inner, name)

        def slow(*a: Any, **k: Any) -> Any:
            time.sleep(0.3)
            return fn(*a, **k)

        return slow


def test_a_slow_store_never_delays_a_message(tmp_path: Path) -> None:
    store = Store(tmp_path / "g.db")
    r = Recorder(Slow(store), "c", "s")  # type: ignore[arg-type]
    started = time.monotonic()
    for i in range(5):
        r.note_client(json.dumps({"jsonrpc": "2.0", "id": i, "method": "tools/call", "params": {"name": "a", "arguments": {}}}).encode())
        r.from_server(json.dumps({"jsonrpc": "2.0", "id": i, "result": {"content": []}}).encode())
    assert time.monotonic() - started < 0.2, "parsing and queueing only"
    r.close()
    assert [row["outcome"] for row in store.calls(0)[0]] == ["ok"] * 5


def test_calls_without_an_answer_are_marked_when_the_session_ends(tmp_path: Path) -> None:
    store = Store(tmp_path / "g.db")
    r = Recorder(store, "c", "s")
    r.note_client(b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"slow","arguments":{}}}\n')
    r.close()
    row = store.calls(0)[0][0]
    assert row["outcome"] == "no response" and "ended" in row["result"]


def test_run_in_process_forwards_and_ends_with_the_client(tmp_path: Path) -> None:
    from toolpass import _mcp_proxy

    data = b"".join(json.dumps(m).encode() + b"\n" for m in SESSION[:4])
    out = io.BytesIO()
    code = _mcp_proxy.run([sys.executable, FAKE], server="fake", client="t", store=Store(tmp_path / "g.db"), stdin=io.BytesIO(data), stdout=out)
    assert code == 0
    assert [json.loads(line)["id"] for line in out.getvalue().splitlines()] == [1, 2, 3]

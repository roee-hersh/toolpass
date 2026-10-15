"""``toolpass mcp``: stand between an MCP client and a stdio MCP server, and
record every tool call that goes through.

    toolpass mcp -server github -client claude-desktop -- npx -y @modelcontextprotocol/server-github

The real server runs as a child process. Every line in either direction is
forwarded unchanged, byte for byte, so the client sees the server's own
tools, schemas and results. On the way, ``tools/call`` requests and their
responses, and ``tools/list`` results (for the tools' annotations), are
copied into the gateway's store. Nothing is held, changed or refused, and a
store that cannot be written never stops the forwarding.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from typing import IO, Any

from toolpass._audit import shorten
from toolpass._store import Store

__all__ = ["Recorder", "run"]

log = logging.getLogger("toolpass.mcp")

_GRACE = 10.0  # seconds a server may take to exit after its client left


def _result_text(result: Any) -> str:
    """The text a tool result carries, for the record."""
    if isinstance(result, Mapping):
        content = result.get("content")
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, Mapping):
                    if item.get("type") == "text" and isinstance(item.get("text"), str):
                        parts.append(item["text"])
                    else:
                        parts.append(f"[{item.get('type', 'content')}]")
            if parts:
                return "\n".join(parts)
        structured = result.get("structuredContent")
        if structured is not None:
            return json.dumps(structured, default=repr, ensure_ascii=False)
    return json.dumps(result, default=repr, ensure_ascii=False)


class _Pending:
    """A tool call the client sent: registered before it is forwarded, so a
    fast answer always finds it; ``ready`` is set once its row exists."""

    def __init__(self, tool: str, arguments: Mapping[str, Any], start: float) -> None:
        self.tool, self.arguments, self.start = tool, arguments, start
        self.call_id: int | None = None
        self.ready = threading.Event()


class Recorder:
    """Watches the JSON-RPC lines of one client-server session and writes the
    tool calls to the store. Thread-safe: one thread feeds each direction.

    ``note_client(line)`` runs before a client line is forwarded and only
    parses it (no I/O); ``from_client(line)`` runs after and writes the rows.
    ``from_server(line)`` runs after a server line is forwarded."""

    _WAIT = 5.0  # seconds a result waits for its call's row to be written

    def __init__(self, store: Store | None, client: str, server: str, clock: Callable[[], float] = time.monotonic) -> None:
        self.store = store
        self.client, self.server = client, server
        self.session = uuid.uuid4().hex[:16]
        self._clock = clock
        self._lock = threading.Lock()
        self._calls: dict[str, _Pending] = {}  # request id -> the call
        self._new: dict[bytes, list[_Pending]] = {}  # noted, not yet written, by line
        self._lists: set[str] = set()  # ids of tools/list requests
        self._warned = False

    @staticmethod
    def _key(msg_id: Any) -> str | None:
        if isinstance(msg_id, bool) or not isinstance(msg_id, (str, int)):
            return None
        return f"{type(msg_id).__name__}:{msg_id}"

    def _messages(self, line: bytes) -> list[Mapping[str, Any]]:
        try:
            doc = json.loads(line)
        except ValueError:
            return []
        items = doc if isinstance(doc, list) else [doc]
        return [m for m in items if isinstance(m, Mapping)]

    def _guard(self, what: str, fn: Callable[..., Any], *args: Any) -> Any:
        try:
            return fn(*args)
        except Exception as e:  # noqa: BLE001 - recording must never break the session
            if not self._warned:
                self._warned = True
                print(f"toolpass: could not record {what} ({type(e).__name__}: {e}); calls still go through", file=sys.stderr, flush=True)
            return None

    def note_client(self, line: bytes) -> None:
        noted: list[_Pending] = []
        for msg in self._messages(line):
            key = self._key(msg.get("id"))
            if key is None:
                continue
            method = msg.get("method")
            if method == "tools/list":
                with self._lock:
                    self._lists.add(key)
            elif method == "tools/call":
                raw_params = msg.get("params")
                params: Mapping[str, Any] = raw_params if isinstance(raw_params, Mapping) else {}
                raw_tool, raw_args = params.get("name"), params.get("arguments")
                tool: str = raw_tool if isinstance(raw_tool, str) else "?"
                arguments: Mapping[str, Any] = raw_args if isinstance(raw_args, Mapping) else {}
                pending = _Pending(tool, arguments, self._clock())
                with self._lock:
                    self._calls[key] = pending
                noted.append(pending)
        if noted:
            with self._lock:
                self._new.setdefault(line, []).extend(noted)

    def from_client(self, line: bytes) -> None:
        with self._lock:
            noted = self._new.pop(line, [])
        for pending in noted:
            try:
                if self.store is not None:
                    args = shorten(pending.arguments)
                    pending.call_id = self._guard("a tool call", self.store.start_call, self.client, self.server, self.session, pending.tool, args)
            finally:
                pending.ready.set()

    def from_server(self, line: bytes) -> None:
        for msg in self._messages(line):
            if "method" in msg:
                continue  # a request or notification from the server, not a response
            key = self._key(msg.get("id"))
            if key is None:
                continue
            with self._lock:
                call = self._calls.pop(key, None)
                listed = key in self._lists
                self._lists.discard(key)
            if call is not None:
                elapsed = (self._clock() - call.start) * 1000
                if "error" in msg:
                    err = msg.get("error")
                    text = err.get("message", "") if isinstance(err, Mapping) else str(err)
                    outcome = "error"
                else:
                    result = msg.get("result")
                    text = _result_text(result)
                    outcome = "error" if isinstance(result, Mapping) and result.get("isError") is True else "ok"
                call.ready.wait(self._WAIT)
                if self.store is not None and call.call_id is not None:
                    self._guard("a tool result", self.store.finish_call, call.call_id, outcome, str(text), elapsed)
            elif listed:
                result = msg.get("result")
                tools = result.get("tools") if isinstance(result, Mapping) else None
                if isinstance(tools, list) and self.store is not None:
                    self._guard("the tool list", self.store.record_tools, self.client, self.server, tools)


def _pump(src: IO[bytes], dst: IO[bytes], watch: Callable[[bytes], None], on_eof: Callable[[], None], note: Callable[[bytes], None] | None = None) -> None:
    try:
        while True:
            line = src.readline()
            if not line:
                break
            if note is not None:
                note(line)  # parse only, before forwarding, so the answer always finds its call
            try:
                dst.write(line)
                dst.flush()
            except (BrokenPipeError, OSError, ValueError):
                break
            watch(line)  # after forwarding: the record never delays the message
    finally:
        on_eof()


def _resolve(command: Sequence[str]) -> list[str]:
    """The command with its program found on PATH (``npx`` is ``npx.cmd`` on
    Windows, which Popen does not find by itself)."""
    found = shutil.which(command[0])
    return [found or command[0], *command[1:]]


def run(
    command: Sequence[str],
    *,
    server: str,
    client: str,
    store: Store | None,
    stdin: IO[bytes] | None = None,
    stdout: IO[bytes] | None = None,
) -> int:
    """Run ``command`` as the real server and forward stdio until either side
    ends; returns the server's exit code."""
    stdin = stdin if stdin is not None else sys.stdin.buffer
    stdout = stdout if stdout is not None else sys.stdout.buffer
    recorder = Recorder(store, client, server)
    try:
        child = subprocess.Popen(_resolve(command), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=None, bufsize=0)
    except OSError as e:
        print(f"toolpass: could not start {command[0]!r}: {e}", file=sys.stderr, flush=True)
        return 127
    assert child.stdin is not None and child.stdout is not None
    child_in, child_out = child.stdin, child.stdout

    def close_child_stdin() -> None:
        # The client went away: the server sees EOF and should exit; one that
        # does not is stopped after a grace period, so no orphan is left behind.
        try:
            child_in.close()
        except OSError:
            pass

        def stop() -> None:
            if child.poll() is None:
                child.terminate()

        timer = threading.Timer(_GRACE, stop)
        timer.daemon = True
        timer.start()

    up = threading.Thread(
        target=_pump, args=(stdin, child_in, recorder.from_client, close_child_stdin, recorder.note_client), name="toolpass-mcp-up", daemon=True
    )
    down = threading.Thread(target=_pump, args=(child_out, stdout, recorder.from_server, lambda: None), name="toolpass-mcp-down", daemon=True)
    up.start()
    down.start()
    try:
        down.join()  # the server closed its stdout: it is done
        code = child.wait()
    except KeyboardInterrupt:
        child.terminate()
        code = child.wait()
    return code

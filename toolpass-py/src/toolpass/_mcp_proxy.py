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
import os
import queue
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
    """A tool call the client sent, registered before it is forwarded so a
    fast answer always finds it. ``call_id`` is set by the writer."""

    def __init__(self, tool: str, arguments: Mapping[str, Any], start: float) -> None:
        self.tool, self.arguments, self.start = tool, arguments, start
        self.sent = time.time()  # wall clock, for the record
        self.t0 = time.perf_counter()  # the clock the server's tool events are placed by
        self.call_id: int | None = None
        self.finished = False


class Recorder:
    """Watches the JSON-RPC lines of one client-server session and records
    the tool calls. The pump threads only parse and queue; one writer thread
    does every store write, in order, so a result is never written before
    its call and a slow or locked store never delays a message.

    ``note_client(line)`` runs before a client line is forwarded,
    ``from_server(line)`` after a server line is forwarded; ``close()``
    marks calls that never got an answer and drains the queue."""

    _DRAIN = 10.0  # seconds close() waits for the writer to catch up

    def __init__(self, store: Store | None, client: str, server: str, config: str = "", clock: Callable[[], float] = time.monotonic) -> None:
        self.store = store
        self.client, self.server, self.config = client, server, config
        self.session = uuid.uuid4().hex[:16]
        self._clock = clock
        self._lock = threading.Lock()
        self._calls: dict[str, _Pending] = {}  # request id -> the call
        self._lists: set[str] = set()  # ids of tools/list requests
        self._warned = False
        self._queue: queue.Queue[tuple[Any, ...] | None] = queue.Queue()
        self._writer = threading.Thread(target=self._write, name="toolpass-mcp-record", daemon=True)
        if store is not None:
            self._writer.start()

    @staticmethod
    def _key(msg_id: Any) -> str | None:
        if isinstance(msg_id, bool) or not isinstance(msg_id, (str, int)):
            return None
        return f"{type(msg_id).__name__}:{msg_id}"

    def _messages(self, line: bytes) -> list[Mapping[str, Any]]:
        try:
            doc = json.loads(line)
        except (ValueError, RecursionError):  # a line nested past the parser's limit is forwarded, not recorded
            return []
        items = doc if isinstance(doc, list) else [doc]
        return [m for m in items if isinstance(m, Mapping)]

    def _put(self, *item: Any) -> None:
        if self.store is not None:
            self._queue.put(item)

    def _write(self) -> None:
        store = self.store
        assert store is not None
        while True:
            item = self._queue.get()
            if item is None:
                return
            try:
                kind = item[0]
                if kind == "start":
                    pending = item[1]
                    pending.call_id = store.start_call(
                        self.client, self.config, self.server, self.session, pending.tool, shorten(pending.arguments), started=pending.sent, t0=pending.t0
                    )
                elif kind == "finish":
                    pending, outcome, text, elapsed, ended, t1 = item[1:]
                    if pending.call_id is not None:
                        store.finish_call(pending.call_id, outcome, text, elapsed, ended=ended, t1=t1)
                elif kind == "tools":
                    store.record_tools(self.client, self.config, self.server, item[1])
            except Exception as e:  # noqa: BLE001 - recording must never break the session
                if not self._warned:
                    self._warned = True
                    print(f"toolpass: could not record a call ({type(e).__name__}: {e}); calls still go through", file=sys.stderr, flush=True)

    def note_client(self, line: bytes) -> None:
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
                self._put("start", pending)

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
                call.finished = True
                self._put("finish", call, outcome, str(text), elapsed, time.time(), time.perf_counter())
            elif listed:
                result = msg.get("result")
                tools = result.get("tools") if isinstance(result, Mapping) else None
                if isinstance(tools, list):
                    self._put("tools", tools)

    def close(self) -> None:
        """The session ended: calls still waiting are recorded as never
        answered, and the writer finishes what is queued."""
        with self._lock:
            left = [c for c in self._calls.values() if not c.finished]
            self._calls.clear()
        for call in left:
            self._put(
                "finish",
                call,
                "no response",
                "the session ended before the server answered",
                (self._clock() - call.start) * 1000,
                time.time(),
                time.perf_counter(),
            )
        if self.store is not None and self._writer.is_alive():
            self._queue.put(None)
            self._writer.join(self._DRAIN)


def _pump(
    src: IO[bytes], dst: IO[bytes], on_eof: Callable[[], None], before: Callable[[bytes], None] | None = None, after: Callable[[bytes], None] | None = None
) -> None:
    """Copy ``src`` to ``dst`` line by line. ``before`` sees a line before it
    is forwarded, ``after`` once it is; both only parse and queue."""
    try:
        while True:
            line = src.readline()
            if not line:
                break
            if before is not None:
                before(line)
            try:
                dst.write(line)
                dst.flush()
            except (BrokenPipeError, OSError, ValueError):
                break
            if after is not None:
                after(line)
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
    config: str = "",
    stdin: IO[bytes] | None = None,
    stdout: IO[bytes] | None = None,
) -> int:
    """Run ``command`` as the real server and forward stdio until either side
    ends; returns the server's exit code."""
    stdin = stdin if stdin is not None else sys.stdin.buffer
    stdout = stdout if stdout is not None else sys.stdout.buffer
    recorder = Recorder(store, client, server, config)
    try:
        env = dict(os.environ)
        if store is not None:
            # A server built with toolpass reports what its tools did inside each call (toolpass._gateway_sink).
            env["TOOLPASS_GATEWAY_SESSION"] = recorder.session
            env["TOOLPASS_GATEWAY_DB"] = str(store.path)
        child = subprocess.Popen(_resolve(command), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=None, env=env)
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

    up = threading.Thread(target=_pump, args=(stdin, child_in, close_child_stdin, recorder.note_client, None), name="toolpass-mcp-up", daemon=True)
    down = threading.Thread(target=_pump, args=(child_out, stdout, lambda: None, None, recorder.from_server), name="toolpass-mcp-down", daemon=True)
    up.start()
    down.start()
    try:
        down.join()  # the server closed its stdout: it is done
        code = child.wait()
    except KeyboardInterrupt:
        child.terminate()
        code = child.wait()
    finally:
        recorder.close()
    return code

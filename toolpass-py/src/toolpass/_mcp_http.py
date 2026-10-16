"""``toolpass mcp -url``: stand between an MCP client and an MCP server reached
over HTTP (the Streamable HTTP transport), and record every tool call.

    toolpass mcp -server github -client claude-code -url https://api.githubcopilot.com/mcp/

The client starts this as a stdio server; ``toolpass gateway on`` writes the
entry. Each JSON-RPC line the client sends is POSTed to the URL with the
entry's own headers (``TOOLPASS_MCP_HEADERS``, a JSON object), and each
message the server answers with, as JSON or as an event stream, is written
back as one line. Messages are forwarded as the server sent them; the same
``Recorder`` as for stdio servers copies the tool calls into the store.

Not carried: the server's standalone GET stream (messages a server sends
outside any request reach the client only within a response), and sign-in
through OAuth, which ``toolpass gateway on`` detects and leaves unwrapped.
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator, Mapping
from typing import IO, Any

from toolpass._mcp_proxy import Recorder
from toolpass._store import Store

__all__ = ["HEADERS_ENV", "expand", "headers_from_env", "probe", "run"]

HEADERS_ENV = "TOOLPASS_MCP_HEADERS"
_TIMEOUT = 300.0  # seconds without a byte from the server before a request is given up
_PROBE_TIMEOUT = 5.0
_PROTOCOL = "2025-06-18"
_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def expand(value: str, env: Mapping[str, str] | None = None) -> str:
    """``${VAR}`` and ``${VAR:-default}`` from the environment, as Claude Code
    expands them in a server entry."""
    env = os.environ if env is None else env
    return _VAR.sub(lambda m: env.get(m.group(1)) or (m.group(2) or ""), value)


def headers_from_env(env: Mapping[str, str] | None = None) -> dict[str, str]:
    env = os.environ if env is None else env
    raw = env.get(HEADERS_ENV, "")
    try:
        doc = json.loads(raw) if raw else {}
    except ValueError:
        return {}
    if not isinstance(doc, dict):
        return {}
    return {str(k): expand(str(v), env) for k, v in doc.items() if isinstance(k, str) and isinstance(v, (str, int))}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect is an error: following it would send the entry's credentials
    wherever the server points."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def _post(url: str, headers: Mapping[str, str], body: bytes, timeout: float) -> Any:
    req = urllib.request.Request(url, data=body, method="POST")
    for k, v in headers.items():
        req.add_header(k, v)
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json, text/event-stream")
    return _opener.open(req, timeout=timeout)


def _events(resp: Any) -> Iterator[bytes]:
    """The data of each event in a text/event-stream response."""
    data: list[bytes] = []
    while True:
        line = resp.readline()
        if not line:
            break
        line = line.rstrip(b"\r\n")
        if not line:
            if data:
                yield b"\n".join(data)
                data = []
        elif line.startswith(b"data:"):
            chunk = line[5:]
            data.append(chunk[1:] if chunk.startswith(b" ") else chunk)
    if data:
        yield b"\n".join(data)


def probe(url: str, headers: Mapping[str, str]) -> str:
    """Whether a server can be reached with the entry's own headers: ``ok``;
    ``sign-in`` when it asks for credentials the entry does not carry (OAuth,
    which the client does by itself); or ``unreachable``."""
    init = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {"protocolVersion": _PROTOCOL, "capabilities": {}, "clientInfo": {"name": "toolpass", "version": "1"}},
    }
    try:
        resp = _post(url, headers, json.dumps(init).encode(), _PROBE_TIMEOUT)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return "ok" if any(k.lower() in ("authorization", "x-api-key", "api-key") for k in headers) else "sign-in"
        return "ok" if e.code < 500 and e.code != 404 else "unreachable"
    except (OSError, ValueError):
        return "unreachable"
    with resp:
        session = resp.headers.get("Mcp-Session-Id")
    if session:  # end the session the probe opened
        try:
            req = urllib.request.Request(url, method="DELETE", headers={**headers, "Mcp-Session-Id": session})
            _opener.open(req, timeout=_PROBE_TIMEOUT).close()
        except (OSError, ValueError):
            pass
    return "ok"


class _Bridge:
    def __init__(self, url: str, headers: Mapping[str, str], out: IO[bytes], recorder: Recorder) -> None:
        self.url, self.headers, self.out, self.recorder = url, dict(headers), out, recorder
        self.session: str | None = None
        self.protocol: str | None = None
        self._write_lock = threading.Lock()

    def _emit(self, data: bytes) -> None:
        line = data.replace(b"\r", b"").replace(b"\n", b"") + b"\n"
        with self._write_lock:
            try:
                self.out.write(line)
                self.out.flush()
            except (BrokenPipeError, OSError, ValueError):
                return
        self.recorder.from_server(line)

    def _fail(self, msg: Any, why: str) -> None:
        """Answer a request the server could not take, so the client is not left waiting."""
        if isinstance(msg, Mapping) and "method" in msg and msg.get("id") is not None:
            err = {"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32000, "message": f"toolpass could not reach the server: {why}"}}
            self._emit(json.dumps(err).encode())

    def send(self, line: bytes) -> None:
        try:
            msg: Any = json.loads(line)
        except (ValueError, RecursionError):
            return  # not JSON-RPC: nothing to forward to an HTTP server
        headers = dict(self.headers)
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        if self.protocol:
            headers["MCP-Protocol-Version"] = self.protocol
        try:
            resp = _post(self.url, headers, line.strip(), _TIMEOUT)
        except urllib.error.HTTPError as e:
            detail = e.read(300).decode("utf-8", "replace").strip() if e.fp else ""
            self._fail(msg, f"HTTP {e.code} {e.reason}" + (f": {detail}" if detail else ""))
            return
        except (OSError, ValueError) as e:
            self._fail(msg, f"{type(e).__name__}: {e}")
            return
        with resp:
            if isinstance(msg, Mapping) and msg.get("method") == "initialize":
                self.session = resp.headers.get("Mcp-Session-Id") or self.session
            kind = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            try:
                if kind == "text/event-stream":
                    for data in _events(resp):
                        self._note_protocol(data)
                        self._emit(data)
                elif kind == "application/json":
                    data = resp.read()
                    if data.strip():
                        self._note_protocol(data)
                        self._emit(data)
            except (OSError, ValueError) as e:
                self._fail(msg, f"the response broke off ({type(e).__name__})")

    def _note_protocol(self, data: bytes) -> None:
        if self.protocol is not None:
            return
        try:
            doc = json.loads(data)
        except (ValueError, RecursionError):
            return
        result = doc.get("result") if isinstance(doc, dict) else None
        version = result.get("protocolVersion") if isinstance(result, dict) else None
        if isinstance(version, str):
            self.protocol = version

    def close(self) -> None:
        if self.session:
            try:
                req = urllib.request.Request(self.url, method="DELETE", headers={**self.headers, "Mcp-Session-Id": self.session})
                _opener.open(req, timeout=_PROBE_TIMEOUT).close()
            except (OSError, ValueError):
                pass


def _is_initialize(line: bytes) -> bool:
    try:
        msg = json.loads(line)
    except (ValueError, RecursionError):
        return False
    return isinstance(msg, dict) and msg.get("method") == "initialize"


def run(
    url: str,
    *,
    server: str,
    client: str,
    store: Store | None,
    config: str = "",
    headers: Mapping[str, str] | None = None,
    stdin: IO[bytes] | None = None,
    stdout: IO[bytes] | None = None,
) -> int:
    """Forward the client's messages to ``url`` until the client leaves."""
    stdin = stdin if stdin is not None else sys.stdin.buffer
    stdout = stdout if stdout is not None else sys.stdout.buffer
    recorder = Recorder(store, client, server, config)
    bridge = _Bridge(url, headers_from_env() if headers is None else headers, stdout, recorder)
    workers: list[threading.Thread] = []
    try:
        while True:
            line = stdin.readline()
            if not line:
                break
            if not line.strip():
                continue
            recorder.note_client(line)
            if _is_initialize(line):
                bridge.send(line)  # the session id it returns goes on every later request
                continue
            t = threading.Thread(target=bridge.send, args=(line,), name="toolpass-mcp-http", daemon=True)
            t.start()
            workers.append(t)
            workers = [w for w in workers if w.is_alive()]
        for w in workers:
            w.join(10.0)
    except KeyboardInterrupt:
        pass
    finally:
        bridge.close()
        recorder.close()
    return 0

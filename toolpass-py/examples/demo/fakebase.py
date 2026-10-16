"""The common part of the demo's stand-in systems: an HTTP server on the
loopback interface answering JSON, and a log of what reached it.

Each system (``fake_github.py``, ``fake_jira.py``, ...) subclasses ``Fake``
and answers both the calls toolpass makes to check a permission and the calls
the demo's tools make to do the work, from the people and permissions in
``world.py``."""

from __future__ import annotations

import json
import threading
import urllib.parse
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


@dataclass
class Req:
    method: str
    path: str  # decoded, without the query
    query: dict[str, str]
    headers: dict[str, str]  # lower-case names
    body: Any  # the parsed JSON body, the raw bytes when not JSON, or None


@dataclass
class Reply:
    status: int = 200
    body: Any = None  # JSON-encoded unless bytes or str
    headers: dict[str, str] = field(default_factory=dict)


class Fake:
    """One stand-in system. Override ``handle``; call ``start`` once."""

    name = "fake"

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.seen: list[str] = []  # "METHOD /path" of every request, in order
        self.url = ""
        self._srv: ThreadingHTTPServer | None = None

    def handle(self, r: Req) -> Reply:
        return Reply(404, {"message": "not found"})

    def start(self) -> str:
        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args: Any) -> None:
                pass

            def _any(self) -> None:
                u = urllib.parse.urlsplit(self.path)
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b""
                body: Any = None
                if raw:
                    try:
                        body = json.loads(raw)
                    except ValueError:
                        body = raw
                req = Req(
                    self.command,
                    urllib.parse.unquote(u.path),
                    dict(urllib.parse.parse_qsl(u.query, keep_blank_values=True)),
                    {k.lower(): v for k, v in self.headers.items()},
                    body,
                )
                with outer.lock:
                    outer.seen.append(f"{self.command} {u.path}")
                try:
                    rep = outer.handle(req)
                except Exception as e:  # noqa: BLE001 - a broken fake answers 500, as a broken server would
                    rep = Reply(500, {"message": f"{type(e).__name__}: {e}"})
                out = rep.body if isinstance(rep.body, bytes) else (rep.body.encode() if isinstance(rep.body, str) else json.dumps(rep.body).encode())
                self.send_response(rep.status)
                ctype = "application/json" if not isinstance(rep.body, (bytes, str)) else "text/plain"
                headers = {"Content-Type": ctype, **rep.headers}
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = _any  # noqa: N815 - the names http.server calls

        self._srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self._srv.daemon_threads = True
        threading.Thread(target=self._srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self._srv.server_address[1]}"
        return self.url

    def stop(self) -> None:
        if self._srv is not None:
            self._srv.shutdown()
            self._srv.server_close()
            self._srv = None

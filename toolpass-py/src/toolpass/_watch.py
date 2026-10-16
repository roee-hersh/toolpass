"""A live page for a toolkit: every call as it happens.

    import toolpass

    page = toolpass.watch()          # the default toolkit; or toolpass.watch(kit)
    print(page.url)                  # http://127.0.0.1:PORT/t/<token>/

The page lists each call the toolkit decided (ran, refused or raised), with
who asked, the arguments, the reason, and the requests the body made. It is
read-only: it changes nothing in the toolkit.

The page binds to the loopback interface only, and every path carries a
random token, so another program or web page on the machine cannot read
anything without the printed URL. To show it to people elsewhere,
put something that authenticates them in front of it; the page itself has
no accounts.
"""

from __future__ import annotations

import hmac
import html
import json
import logging
import os
import pathlib
import secrets
import shutil
import socket
import sys
import tempfile
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

from toolpass._audit import AuditEvent, AuditSink
from toolpass._default import default_toolkit
from toolpass._page import PAGE as _PAGE
from toolpass._page import SCRIPT as _SCRIPT
from toolpass._page import STYLE as _STYLE
from toolpass._toolkit import Toolkit

__all__ = ["Watch", "open_in_browser", "watch"]  # _Page is shared with the dashboard

log = logging.getLogger("toolpass.watch")

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
_MAX_BODY = 64 * 1024


def watch(toolkit: Toolkit | None = None, *, listen: str = "127.0.0.1:0", open_browser: bool = False) -> Watch:
    """Serve the live page for ``toolkit`` (the default toolkit when None)
    and return it; ``.url`` is the address to open. ``listen`` is
    ``host:port`` on the loopback interface, port 0 for any free one.
    ``open_browser`` opens the page in the default browser."""
    page = Watch(toolkit if toolkit is not None else default_toolkit(), listen=listen)
    if open_browser:
        open_in_browser(page.url)
    return page


def open_in_browser(url: str) -> str:
    """Open ``url`` in the default browser without putting it on a command
    line: ``webbrowser`` hands the browser its argument through ``xdg-open``
    and the browser's own argv, both readable by other local users in
    ``ps``. The browser gets a file only this user can read, which forwards
    to the page and is deleted shortly after. Returns the file's path."""
    folder = tempfile.mkdtemp(prefix="toolpass-watch-")
    path = os.path.join(folder, "open.html")
    body = f'<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" content="0;url={html.escape(url, quote=True)}"><title>toolpass</title>'
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(body)
    webbrowser.open(pathlib.Path(path).as_uri())  # file:///..., on Windows too

    def tidy() -> None:
        time.sleep(30)
        shutil.rmtree(folder, ignore_errors=True)

    threading.Thread(target=tidy, name="toolpass-watch-tidy", daemon=True).start()
    return path


class _Page:
    """A read-only page on the loopback interface, behind a random token.
    Subclasses say what it shows: ``events(since)``. ``url`` is its address;
    ``close()`` stops it.
    Usable as a context manager."""

    kind = "toolkit"
    title = "toolpass"

    def __init__(self, *, listen: str = "127.0.0.1:0") -> None:
        host, _, port = listen.rpartition(":")
        host = host.strip("[]") or "127.0.0.1"
        if host not in _LOOPBACK_HOSTS:
            raise ValueError(f"the page binds to the loopback interface only, not {host!r}")
        self._token = secrets.token_urlsafe(24)
        self._closed = False
        server_class = _V6Server if host == "::1" else _V4Server
        self._httpd = server_class((host if host != "localhost" else "127.0.0.1", int(port or 0)), self._handler_class())
        self._httpd.daemon_threads = True
        bound_host, bound_port = str(self._httpd.server_address[0]), int(self._httpd.server_address[1])
        shown = f"[{bound_host}]" if ":" in bound_host else bound_host
        self.url = f"http://{shown}:{bound_port}/t/{self._token}/"
        self._thread = threading.Thread(target=self._httpd.serve_forever, kwargs={"poll_interval": 0.2}, name="toolpass-page", daemon=True)
        self._thread.start()

    def events(self, since: int = 0) -> tuple[list[dict[str, Any]], int]:
        """Rows added or changed after ``since``, and the number to pass next time."""
        raise NotImplementedError

    def _detach(self) -> None:
        pass

    def close(self) -> None:
        """Stop the server."""
        if self._closed:
            return
        self._closed = True
        self._detach()
        self._httpd.shutdown()
        self._httpd.server_close()

    def __enter__(self) -> _Page:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # -- the HTTP side --------------------------------------------------------

    def _handler_class(self) -> type[BaseHTTPRequestHandler]:
        page = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "toolpass-watch"
            sys_version = ""
            protocol_version = "HTTP/1.1"
            timeout = 15

            def log_message(self, format: str, *args: Any) -> None:  # quiet
                pass

            def do_GET(self) -> None:
                self._serve("GET")

            def _serve(self, method: str) -> None:
                status, kind, body = page._respond(method, self.path, self.headers.get("Host", ""), self._discard_body())
                self.send_response(status)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("X-Frame-Options", "DENY")
                self.send_header("Referrer-Policy", "no-referrer")
                if kind.startswith("text/html"):
                    self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'")
                self.end_headers()
                self.wfile.write(body)

            def _discard_body(self) -> bool:
                """Read and drop a request body; False when the request is
                not framed by one plain Content-Length, or is too large (the
                connection is then closed instead of read, so nothing of it
                is taken for the next request)."""
                # One framing only, stated once, as the main server: a chunked
                # body, two lengths or a length that is not digits closes the
                # connection rather than leaving bytes behind on it.
                tes = self.headers.get_all("Transfer-Encoding") or []
                cls = self.headers.get_all("Content-Length") or []
                if tes or len(cls) > 1 or (cls and not (cls[0].isascii() and cls[0].isdigit())):
                    self.close_connection = True
                    return False
                n = int(cls[0]) if cls else 0
                if n > _MAX_BODY:
                    self.close_connection = True
                    return False
                while n > 0:
                    chunk = self.rfile.read(min(n, 8192))
                    if not chunk:
                        break
                    n -= len(chunk)
                return True

        return Handler

    def _respond(self, method: str, path: str, host: str, body_ok: bool) -> tuple[int, str, bytes]:
        if not _loopback(host):
            return 403, "text/plain; charset=utf-8", b"forbidden\n"
        if not body_ok:
            return 413, "text/plain; charset=utf-8", b"too large\n"
        parts = urlsplit(path)
        segments = parts.path.split("/")
        # Bytes, not str: compare_digest raises on a non-ASCII str, and the request line can carry one.
        if len(segments) < 4 or segments[1] != "t" or not hmac.compare_digest(segments[2].encode("utf-8", "surrogateescape"), self._token.encode()):
            return 404, "text/plain; charset=utf-8", b"not found\n"
        rest = segments[3:]
        if method == "GET" and rest == [""]:
            return 200, "text/html; charset=utf-8", _PAGE
        if method == "GET" and rest == ["watch.js"]:
            return 200, "text/javascript; charset=utf-8", _SCRIPT
        if method == "GET" and rest == ["watch.css"]:
            return 200, "text/css; charset=utf-8", _STYLE
        if method == "GET" and rest == ["events"]:
            query = parse_qs(parts.query)
            try:
                since = int(query.get("since", ["0"])[0])
            except ValueError:
                since = 0
            events, nxt = self.events(since)
            payload = {"kind": self.kind, "title": self.title, "events": events, "next": nxt}
            return 200, "application/json", json.dumps(payload, default=repr, ensure_ascii=False).encode()
        return 404, "text/plain; charset=utf-8", b"not found\n"


class Watch(_Page):
    """The live page of one toolkit. It replaces the toolkit's audit sink
    with one that records each call and then hands it on to the old sink;
    ``close()`` puts the old sink back."""

    def __init__(self, toolkit: Toolkit, *, listen: str = "127.0.0.1:0", keep: int = 1000) -> None:
        self.toolkit = toolkit
        self._lock = threading.Lock()
        self._events: list[dict[str, Any]] = []
        self._seq = 0
        self._keep = keep
        super().__init__(listen=listen)  # first: a bad address leaves the toolkit untouched
        self._previous_audit: AuditSink | None = toolkit.audit
        self._sink: AuditSink = self._audit  # one bound method, so close() can tell it is still ours
        toolkit.audit = self._sink

    def _audit(self, event: AuditEvent) -> None:
        if not self._closed:
            self._record(event)
        if self._previous_audit is not None:
            self._previous_audit(event)

    def _record(self, event: AuditEvent) -> None:
        row = event.to_dict()
        with self._lock:
            self._seq += 1
            row["seq"] = self._seq
            self._events.append(row)
            if len(self._events) > self._keep:
                del self._events[: len(self._events) - self._keep]

    def events(self, since: int = 0) -> tuple[list[dict[str, Any]], int]:
        with self._lock:
            return [e for e in self._events if e["seq"] > since], self._seq

    def _detach(self) -> None:
        if self.toolkit.audit is self._sink:
            self.toolkit.audit = self._previous_audit

    def __enter__(self) -> Watch:
        return self


def _loopback(host_header: str) -> bool:
    """True when the Host header names this machine's loopback interface: a
    page on another origin that resolves a name to 127.0.0.1 (DNS
    rebinding) sends its own host and is refused."""
    host = host_header.strip().lower()
    if host.startswith("["):
        host = host[1 : host.find("]")] if "]" in host else ""
    else:
        host = host.rpartition(":")[0] if host.count(":") == 1 else host
    return host in _LOOPBACK_HOSTS


class _Server(ThreadingHTTPServer):
    allow_reuse_address = False

    def handle_error(self, request: Any, client_address: Any) -> None:
        # A browser tab closing mid-request is not worth a traceback on stderr.
        exc = sys.exc_info()[1]
        if not isinstance(exc, (ConnectionError, TimeoutError)):
            log.debug("toolpass watch: request from %s failed", client_address, exc_info=True)


class _V4Server(_Server):
    address_family = socket.AF_INET


class _V6Server(_Server):
    address_family = socket.AF_INET6

"""Report each run of a toolpass tool to the toolpass server, which collects
them from every agent and shows them at ``/dashboard/``.

A toolkit whose ``permissions`` is ``Toolpass.remote(...)`` already knows the
server and its API key, and reports there with nothing else configured. A
toolkit with an in-process engine reports when ``TOOLPASS_URL`` and
``TOOLPASS_API_KEY`` are set. ``TOOLPASS_DASHBOARD=off`` turns reporting off;
``TOOLPASS_DASHBOARD=https://...`` names another server (with
``TOOLPASS_API_KEY``). ``TOOLPASS_SERVER_NAME`` names this process on the
page (by default, its script's name).

What is sent is the audit event: the tool, the user it ran for, the outcome
and why, and each request the body made as toolpass understood it. One
background thread sends, in batches, so a slow or unreachable server never
delays a tool; the queue is bounded, and what is queued when the process
exits is sent within a couple of seconds.
"""

from __future__ import annotations

import atexit
import collections
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from toolpass._audit import AuditEvent

__all__ = ["ENV", "emit", "target"]

ENV = "TOOLPASS_DASHBOARD"
_OFF = ("0", "false", "off", "no", "none")
_BATCH = 50  # runs per POST
_QUEUE = 10_000  # runs waiting; the oldest go when it is full
_TIMEOUT = 5.0
_PAUSE = 0.2  # seconds to gather a batch before sending

_lock = threading.Lock()
_sender: _Sender | None = None
_warned: set[str] = set()


def target(permissions: Any = None) -> tuple[str, str] | None:
    """Where this process reports its runs: ``(url, api_key)``, or None."""
    raw = os.environ.get(ENV, "").strip()
    if raw.lower() in _OFF:
        return None
    key = os.environ.get("TOOLPASS_API_KEY", "")
    if raw and raw != "1":
        return (raw.rstrip("/"), key) if key else None
    remote = getattr(permissions, "server", None) if permissions is not None else None
    if isinstance(remote, tuple) and len(remote) == 2 and all(isinstance(x, str) for x in remote):
        return remote[0], remote[1]
    url = os.environ.get("TOOLPASS_URL", "")
    return (url.rstrip("/"), key) if url and key else None


def _server_name() -> str:
    name = os.environ.get("TOOLPASS_SERVER_NAME", "").strip()
    if name:
        return name
    script = Path(sys.argv[0]).stem if sys.argv and sys.argv[0] not in ("", "-c", "-m") else ""
    return script or "python"


def _warn(key: str, text: str) -> None:
    if key not in _warned:
        _warned.add(key)
        print(f"toolpass: {text}", file=sys.stderr, flush=True)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect would re-send the API key to whatever host it names."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


class _Sender:
    """One queue and one thread per process, whatever the target: the first
    event decides where every run goes."""

    def __init__(self, url: str, key: str) -> None:
        self.url, self.key = url, key
        self.queue: collections.deque[dict[str, Any]] = collections.deque(maxlen=_QUEUE)
        self.wake = threading.Condition()
        self.closing = False
        self.dropped = 0
        self.opener = urllib.request.build_opener(_NoRedirect())
        self.thread = threading.Thread(target=self._run, name="toolpass-runs", daemon=True)
        self.thread.start()
        atexit.register(self.close)

    def put(self, run: dict[str, Any]) -> None:
        with self.wake:
            if len(self.queue) == self.queue.maxlen:
                self.dropped += 1
            self.queue.append(run)
            self.wake.notify()

    def _run(self) -> None:
        while True:
            with self.wake:
                while not self.queue and not self.closing:
                    self.wake.wait()
                if not self.queue and self.closing:
                    return
                if not self.closing:
                    self.wake.wait(_PAUSE)  # a tool often makes several runs at once: one POST for them
                batch = [self.queue.popleft() for _ in range(min(_BATCH, len(self.queue)))]
                dropped, self.dropped = self.dropped, 0
            if dropped:
                _warn("dropped", f"{dropped} tool run(s) were not reported to the dashboard: the server at {self.url} is not keeping up")
            self._post(batch)

    def _post(self, batch: list[dict[str, Any]]) -> None:
        body = json.dumps({"server": _server_name(), "runs": batch}, default=repr, ensure_ascii=False).encode()
        req = urllib.request.Request(
            self.url + "/runs",
            data=body,
            method="POST",
            headers={"Authorization": "Bearer " + self.key, "Content-Type": "application/json", "Accept": "application/json"},
        )
        for attempt in (1, 2):
            try:
                with self.opener.open(req, timeout=_TIMEOUT) as resp:
                    resp.read(1024)
                return
            except urllib.error.HTTPError as e:
                if e.code in (401, 403):
                    _warn("auth", f"the toolpass server at {self.url} refused this process's API key; its tool runs are not reported")
                    return
                if 400 <= e.code < 500:
                    _warn("rejected", f"the toolpass server at {self.url} did not take a batch of tool runs (HTTP {e.code}); they are not reported")
                    return
            except (OSError, ValueError):
                pass
            if attempt == 1:
                time.sleep(0.5)
        _warn("unreachable", f"the toolpass server at {self.url} did not take a batch of tool runs; they are not reported")

    def close(self) -> None:
        with self.wake:
            self.closing = True
            self.wake.notify()
        self.thread.join(2.0)


def emit(event: AuditEvent, permissions: Any = None) -> None:
    """Queue ``event`` for the toolpass server this process reports to, if any."""
    global _sender
    found = target(permissions)
    if found is None:
        return
    with _lock:
        sender = _sender
        if sender is None or sender.closing:
            sender = _sender = _Sender(*found)
    sender.put({**event.to_dict(), "at": time.time()})

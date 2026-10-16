"""Send each run of a toolpass tool to the dashboard's record.

Set ``TOOLPASS_DASHBOARD`` in the environment of a process that uses toolpass
tools (an MCP server, an agent) and every run is written there: the tool, the
user it ran for, the outcome and why, and each request the body made to
GitHub, Jira, Slack, ... as toolpass understood it, allowed or refused.
``toolpass dashboard`` shows them. ``TOOLPASS_DASHBOARD=1`` writes to the
record ``toolpass dashboard`` reads by default; any other value is the path of
the file. ``TOOLPASS_SERVER_NAME`` names the process on the page (by default,
its script's name).

One background thread writes, so a slow record never delays a tool; what is
queued when the process exits is written within a couple of seconds.
"""

from __future__ import annotations

import atexit
import os
import queue
import sys
import threading
import time
from pathlib import Path
from typing import Any

from toolpass._audit import AuditEvent

__all__ = ["ENV", "emit", "target"]

ENV = "TOOLPASS_DASHBOARD"
_OFF = ("", "0", "false", "off", "no")
_ON = ("1", "true", "on", "yes")

_lock = threading.Lock()
_queue: queue.Queue[tuple[str, dict[str, Any], float] | None] | None = None
_writer: threading.Thread | None = None
_warned = False


def target() -> Path | None:
    """The record runs go to, or None when this process writes none."""
    raw = os.environ.get(ENV, "").strip()
    if raw.lower() in _OFF:
        return None
    if raw.lower() in _ON:
        from toolpass._store import default_store_path

        return default_store_path()
    return Path(raw)


def _server_name() -> str:
    name = os.environ.get("TOOLPASS_SERVER_NAME", "").strip()
    if name:
        return name
    script = Path(sys.argv[0]).stem if sys.argv and sys.argv[0] not in ("", "-c", "-m") else ""
    return script or "python"


def _start(path: Path) -> queue.Queue[tuple[str, dict[str, Any], float] | None]:
    global _queue, _writer
    q: queue.Queue[tuple[str, dict[str, Any], float] | None] = queue.Queue()

    def write() -> None:
        global _warned
        from toolpass._store import Store

        store = None
        while True:
            item = q.get()
            if item is None:
                break
            try:
                if store is None:
                    store = Store(path)
                store.record_run(*item)
            except Exception as e:  # noqa: BLE001 - the record must never break a tool
                if not _warned:
                    _warned = True
                    print(f"toolpass: could not write a run to the dashboard's record ({type(e).__name__}: {e})", file=sys.stderr, flush=True)
        if store is not None:
            store.close()

    _writer = threading.Thread(target=write, name="toolpass-dashboard-record", daemon=True)
    _writer.start()
    _queue = q
    atexit.register(_drain)
    return q


def _drain() -> None:
    if _queue is not None and _writer is not None and _writer.is_alive():
        _queue.put(None)
        _writer.join(2.0)


def emit(event: AuditEvent) -> None:
    """Queue ``event`` for the dashboard's record, when ``TOOLPASS_DASHBOARD``
    is set; otherwise do nothing."""
    path = target()
    if path is None:
        return
    with _lock:
        q = _queue if _queue is not None else _start(path)
    q.put((_server_name(), event.to_dict(), time.time()))

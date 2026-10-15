"""Inside a server the gateway started, send what each secured tool did to
the gateway's record, so the dashboard shows it under the MCP call.

``toolpass mcp`` starts the server with ``TOOLPASS_GATEWAY_SESSION`` (the
session it records the calls under) and ``TOOLPASS_GATEWAY_DB`` (the record).
When both are set, every audit event a toolkit emits is also written there:
the tool, the user, the outcome and why, and each request the body made to
GitHub, Jira, Slack, ... as toolpass understood it. One background thread
writes, so a slow record never delays a tool; what is queued when the server
exits is written within a couple of seconds.
"""

from __future__ import annotations

import atexit
import os
import queue
import sys
import threading
import time
from typing import Any

from toolpass._audit import AuditEvent

__all__ = ["emit"]

_lock = threading.Lock()
_queue: queue.Queue[tuple[str, dict[str, Any], float, float] | None] | None = None
_writer: threading.Thread | None = None
_session: str | None = None
_warned = False


def _target() -> tuple[str, str] | None:
    session, db = os.environ.get("TOOLPASS_GATEWAY_SESSION"), os.environ.get("TOOLPASS_GATEWAY_DB")
    return (session, db) if session and db else None


def _start(db: str) -> queue.Queue[tuple[str, dict[str, Any], float, float] | None]:
    global _queue, _writer
    q: queue.Queue[tuple[str, dict[str, Any], float, float] | None] = queue.Queue()

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
                    store = Store(db)
                store.record_tool_event(*item)
            except Exception as e:  # noqa: BLE001 - the record must never break a tool
                if not _warned:
                    _warned = True
                    print(f"toolpass: could not record a tool call for the gateway ({type(e).__name__}: {e})", file=sys.stderr, flush=True)
        if store is not None:
            store.close()

    _writer = threading.Thread(target=write, name="toolpass-gateway-sink", daemon=True)
    _writer.start()
    _queue = q
    atexit.register(_drain)
    return q


def _drain() -> None:
    if _queue is not None and _writer is not None and _writer.is_alive():
        _queue.put(None)
        _writer.join(2.0)


def emit(event: AuditEvent) -> None:
    """Queue ``event`` for the gateway's record, when this process runs under
    the gateway; otherwise do nothing."""
    target = _target()
    if target is None:
        return
    session, db = target
    with _lock:
        q = _queue if _queue is not None else _start(db)
    # The same clock the gateway times its calls with, read as the run is
    # reported, which is before the server answers: comparable on every system.
    ended = time.time()
    started = ended - (event.duration_ms or 0.0) / 1000
    q.put((session, event.to_dict(), started, ended))

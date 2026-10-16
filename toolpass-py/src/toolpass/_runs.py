"""The runs a toolpass server collects: every run of a secured tool that an
agent or MCP server reports to ``POST /runs``, kept in memory, most recent
first out, and served to the dashboard at ``GET /dashboard/events``.

The log is bounded: the oldest runs go when it is full. It is what the
dashboard shows live; long-term retention is the job of an export
(``toolpass.otel``) into the team's own observability stack.
"""

from __future__ import annotations

import collections
import json
import threading
import time
from collections.abc import Mapping
from typing import Any

__all__ = ["MAX_RUNS_BODY", "RunLog", "decode_runs"]

MAX_RUNS_BODY = 1 << 20  # one POST /runs: a batch of runs
_MAX_RUN = 64 << 10  # one run as JSON; a bigger one keeps its outcome, not its requests
_MAX_BATCH = 500
_MAX_NAME = 200


class _BadRuns(ValueError):
    pass


def decode_runs(body: bytes) -> tuple[str, list[dict[str, Any]]]:
    """The server name and the runs of a ``POST /runs`` body:
    ``{"server": "shop", "runs": [<audit event>, ...]}``. Raises ValueError
    with the reason when the body is not that."""
    try:
        doc = json.loads(body)
    except ValueError as e:
        raise _BadRuns(f"invalid JSON: {e}") from None
    if not isinstance(doc, dict):
        raise _BadRuns("body must be a JSON object")
    server = doc.get("server", "")
    if not isinstance(server, str) or not server.strip() or len(server) > _MAX_NAME:
        raise _BadRuns(f'"server" must be a non-empty string of at most {_MAX_NAME} characters')
    runs = doc.get("runs")
    if not isinstance(runs, list) or not runs or len(runs) > _MAX_BATCH:
        raise _BadRuns(f'"runs" must be a list of 1 to {_MAX_BATCH} runs')
    out: list[dict[str, Any]] = []
    for i, run in enumerate(runs):
        if not isinstance(run, dict) or not isinstance(run.get("tool"), str) or not isinstance(run.get("outcome"), str):
            raise _BadRuns(f'runs[{i}] must be an object with a string "tool" and "outcome"')
        out.append(_slim(run))
    return server.strip(), out


def _slim(run: dict[str, Any]) -> dict[str, Any]:
    """A run as stored; a huge one keeps its outcome and drops its requests."""
    if len(json.dumps(run, default=repr)) <= _MAX_RUN:
        return run
    kept = {k: v for k, v in run.items() if k not in ("actions", "arguments")}
    kept["cut"] = "the requests and arguments were left out: the run was too large"
    return kept


class RunLog:
    """The last ``keep`` runs, in the order they arrived. Safe to share
    between the server's threads."""

    def __init__(self, keep: int = 10_000) -> None:
        self._runs: collections.deque[dict[str, Any]] = collections.deque(maxlen=keep)
        self._seq = 0
        self._lock = threading.Lock()

    def add(self, server: str, run: Mapping[str, Any], at: float | None = None) -> int:
        with self._lock:
            self._seq += 1
            self._runs.append({**run, "seq": self._seq, "server": server, "at": at or time.time()})
            return self._seq

    def runs(self, since: int = 0, limit: int = 500) -> tuple[list[dict[str, Any]], int]:
        """Runs after ``since`` (oldest first, at most ``limit``) and the
        number to pass next time. ``since`` 0 gives the latest ``limit``."""
        with self._lock:
            if since <= 0:
                out = list(self._runs)[-limit:]
            else:
                out = [r for r in self._runs if r["seq"] > since][:limit]
            return out, (out[-1]["seq"] if out else max(since, self._seq if since <= 0 else since))

    def __len__(self) -> int:
        with self._lock:
            return len(self._runs)

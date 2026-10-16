"""The dashboard's record: every run of a toolpass tool, with each request it
made to GitHub, Jira, Slack, ... and whether toolpass let it through, in one
SQLite file shared by every server process that writes to it and the
dashboard that reads it.

A row is written once, when the run is over. Its id only grows, so a reader
asks for "every run after id N".
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import sys
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

__all__ = ["Store", "data_dir", "default_store_path"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  time TEXT NOT NULL,
  at REAL NOT NULL,
  server TEXT NOT NULL,
  event TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS runs_at ON runs (at);
"""

_MAX_EVENT = 64000


def _cut_event(event: Mapping[str, Any]) -> str:
    """The run as JSON; a huge one keeps its outcome and drops its requests."""
    text = json.dumps(event, default=repr, ensure_ascii=False)
    if len(text) <= _MAX_EVENT:
        return text
    slim = {k: v for k, v in event.items() if k not in ("actions", "arguments")}
    slim["cut"] = f"{len(text)} characters; the requests and arguments were left out"
    return json.dumps(slim, default=repr, ensure_ascii=False)


def data_dir() -> Path:
    """Where toolpass keeps its files: ``TOOLPASS_DATA_DIR``, or the platform's
    per-user data folder."""
    override = os.environ.get("TOOLPASS_DATA_DIR")
    if override:
        return Path(override)
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "toolpass"
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        return (Path(base) if base else Path.home() / "AppData" / "Local") / "toolpass"
    base = os.environ.get("XDG_DATA_HOME")
    return (Path(base) if base else Path.home() / ".local" / "share") / "toolpass"


def default_store_path() -> Path:
    return data_dir() / "dashboard.db"


class Store:
    """One connection to the dashboard's SQLite file. Safe to share between
    threads of one process; several processes may open the same file."""

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self.path = Path(path) if path is not None else default_store_path()
        if not self.path.parent.exists():
            # A folder toolpass makes is this user's only: the runs carry arguments.
            self.path.parent.mkdir(parents=True, mode=0o700)
        if not self.path.exists():
            with contextlib.suppress(FileExistsError):
                os.close(os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(self.path), timeout=15, isolation_level=None, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.execute("PRAGMA busy_timeout=15000")
            if self._db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'runs'").fetchone() is None:
                self._db.execute("PRAGMA journal_mode=WAL")
                self._db.executescript(_SCHEMA)
        self._private_side_files()

    def _private_side_files(self) -> None:
        """SQLite's WAL and shared-memory files carry recent rows too: same
        mode as the database."""
        if sys.platform == "win32":
            return
        for suffix in ("-wal", "-shm"):
            side = self.path.with_name(self.path.name + suffix)
            with contextlib.suppress(OSError):
                if side.exists():
                    os.chmod(side, 0o600)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def record_run(self, server: str, event: Mapping[str, Any], at: float | None = None) -> int:
        """One finished tool run (an audit event's ``to_dict()``) of ``server``; returns its id."""
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO runs (time, at, server, event) VALUES (?, ?, ?, ?)",
                (str(event.get("time") or ""), at or time.time(), server, _cut_event(event)),
            )
            run_id = int(cur.lastrowid or 0)
        self._private_side_files()
        return run_id

    def prune(self, days: float = 90) -> int:
        """Delete runs older than ``days``; returns how many."""
        with self._lock:
            return int(self._db.execute("DELETE FROM runs WHERE at < ?", (time.time() - days * 86400,)).rowcount)

    def runs(self, since: int = 0, limit: int = 500) -> tuple[list[dict[str, Any]], int]:
        """Runs after id ``since``, oldest first and at most ``limit``, and the
        id to pass next time. With ``since`` 0, the latest ``limit`` runs, so a
        fresh page does not replay all history."""
        with self._lock:
            if since <= 0:
                rows = self._db.execute("SELECT * FROM (SELECT * FROM runs ORDER BY id DESC LIMIT ?) ORDER BY id", (limit,)).fetchall()
            else:
                rows = self._db.execute("SELECT * FROM runs WHERE id > ? ORDER BY id LIMIT ?", (since, limit)).fetchall()
        out = []
        for r in rows:
            try:
                event = json.loads(r["event"])
            except ValueError:
                continue
            if isinstance(event, dict):
                out.append({**event, "seq": int(r["id"]), "server": r["server"]})
        nxt = int(rows[-1]["id"]) if rows else since
        return out, max(nxt, since)

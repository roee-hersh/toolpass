"""The gateway's record: every MCP server it found and every tool call made
through it, in one SQLite file shared by every wrapped server process and
the dashboard.

Rows are versioned with one counter (``rev``) that every insert and update
advances, so a reader asks for "everything changed since rev N" and sees a
call when it starts and again when its result arrives.
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import sys
import threading
import time
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

__all__ = ["Store", "data_dir", "default_store_path"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v INTEGER NOT NULL);
INSERT OR IGNORE INTO meta (k, v) VALUES ('rev', 0);
CREATE TABLE IF NOT EXISTS servers (
  client TEXT NOT NULL,
  name TEXT NOT NULL,
  config TEXT NOT NULL,
  transport TEXT NOT NULL,
  target TEXT NOT NULL,
  wrapped INTEGER NOT NULL,
  seen_at TEXT NOT NULL,
  rev INTEGER NOT NULL,
  PRIMARY KEY (client, config, name)
);
CREATE TABLE IF NOT EXISTS tools (
  client TEXT NOT NULL,
  config TEXT NOT NULL,
  server TEXT NOT NULL,
  name TEXT NOT NULL,
  annotations TEXT NOT NULL,
  rev INTEGER NOT NULL,
  PRIMARY KEY (client, config, server, name)
);
CREATE TABLE IF NOT EXISTS calls (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  time TEXT NOT NULL,
  client TEXT NOT NULL,
  config TEXT NOT NULL,
  server TEXT NOT NULL,
  session TEXT NOT NULL,
  tool TEXT NOT NULL,
  arguments TEXT NOT NULL,
  outcome TEXT NOT NULL,
  result TEXT,
  duration_ms REAL,
  started REAL NOT NULL DEFAULT 0,
  ended REAL,
  rev INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS calls_rev ON calls (rev);
CREATE INDEX IF NOT EXISTS calls_session ON calls (session, started);
CREATE TABLE IF NOT EXISTS tool_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session TEXT NOT NULL,
  tool TEXT NOT NULL,
  started REAL NOT NULL,
  ended REAL NOT NULL,
  call_id INTEGER,
  event TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS tool_events_session ON tool_events (session, call_id, started);
CREATE INDEX IF NOT EXISTS tool_events_call ON tool_events (call_id);
CREATE INDEX IF NOT EXISTS calls_time ON calls (time);
"""

_MAX_TEXT = 4000
_MAX_EVENT = 64000
# Seconds a tool event's start may precede its call's: the event's time is
# kept to the millisecond, and the wall clock is coarse on some systems.
_SLACK = max(0.002, 2 * time.get_clock_info("time").resolution)


# The call a tool event belongs to, chosen once its candidates are final:
# among the finished calls of its session that were sent before the tool
# started and answered after it ended, one of the same tool first, then one
# that holds no event yet, then the first sent. Only finished calls count, so
# a call whose answer is not recorded yet cannot claim the next call's runs;
# the times are the proxy's own, right however late they are written. Calls one after another leave
# a single candidate; two of one tool at once (parallel tool use) pair up in
# the order they were sent, as a server starts them; a tool the MCP server
# names differently, or a secured tool another one calls, lands on the call
# running at that time.
_OWNER = (
    "SELECT c.id FROM calls c WHERE c.session = :session AND c.started <= :started + :slack"
    " AND c.ended IS NOT NULL AND c.ended >= :ended - :slack"
    " ORDER BY c.tool = :tool DESC, NOT EXISTS (SELECT 1 FROM tool_events e WHERE e.call_id = c.id) DESC, c.started, c.id"
    " LIMIT 1"
)


def _place(db: sqlite3.Connection, rev: int, session: str) -> None:
    """Place the session's unplaced tool events whose call has finished,
    oldest first, so two runs of one tool pair with their calls in order."""
    waiting = db.execute("SELECT id, tool, started, ended FROM tool_events WHERE session = ? AND call_id IS NULL ORDER BY started, id", (session,)).fetchall()
    for e in waiting:
        pending = db.execute(
            "SELECT 1 FROM calls WHERE session = ? AND tool = ? AND ended IS NULL AND started <= ? LIMIT 1", (session, e[1], e[2] + _SLACK)
        ).fetchone()
        if pending is not None:
            continue  # a call of its own tool that could own it has not finished yet
        found = db.execute(_OWNER, {"session": session, "tool": e[1], "started": e[2], "ended": e[3], "slack": _SLACK}).fetchone()
        if found is not None:
            db.execute("UPDATE tool_events SET call_id = ? WHERE id = ?", (found[0], e[0]))
            db.execute("UPDATE calls SET rev = ? WHERE id = ?", (rev, found[0]))


def _cut_event(event: Mapping[str, Any]) -> str:
    text = json.dumps(event, default=repr, ensure_ascii=False)
    if len(text) <= _MAX_EVENT:
        return text
    trimmed = dict(event)
    actions = trimmed.get("actions")
    if isinstance(actions, list):
        trimmed["actions"] = actions[:50]
        trimmed["actions_cut"] = len(actions) - 50
    return json.dumps(trimmed, default=repr, ensure_ascii=False)[:_MAX_EVENT]


def data_dir() -> Path:
    """Where toolpass keeps the gateway's files for this user:
    ``TOOLPASS_DATA_DIR`` when set, else the platform's per-user data folder."""
    override = os.environ.get("TOOLPASS_DATA_DIR")
    if override:
        return Path(override)
    home = Path.home()
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        return Path(base) / "toolpass" if base else home / "AppData" / "Local" / "toolpass"
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / "toolpass"
    base = os.environ.get("XDG_DATA_HOME")
    return (Path(base) if base else home / ".local" / "share") / "toolpass"


def default_store_path() -> Path:
    return data_dir() / "gateway.db"


def _now() -> str:
    t = time.time()
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t)) + f".{int(t * 1000) % 1000:03d}Z"


def _add_column(db: sqlite3.Connection, table: str, column: str, kind: str) -> None:
    """Add a column; one another server opening the same record added first is fine."""
    try:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")  # fixed names, no input
    except sqlite3.OperationalError as e:
        if "duplicate column" not in str(e):
            raise


def _cut(text: str) -> str:
    return text if len(text) <= _MAX_TEXT else text[:_MAX_TEXT] + f"...({len(text)} chars)"


class Store:
    """One connection to the gateway's SQLite file. Safe to share between
    threads of one process; several processes may open the same file."""

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self.path = Path(path) if path is not None else default_store_path()
        if not self.path.parent.exists():
            # A folder toolpass makes is this user's only: the calls carry arguments and results.
            self.path.parent.mkdir(parents=True, mode=0o700)
        if not self.path.exists():
            with contextlib.suppress(FileExistsError):
                os.close(os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(self.path), timeout=15, isolation_level=None, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.execute("PRAGMA busy_timeout=15000")
            if self._db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'tool_events'").fetchone() is None:
                self._db.execute("PRAGMA journal_mode=WAL")
                self._upgrade()
                self._db.executescript(_SCHEMA)
        self._private_side_files()

    def _upgrade(self) -> None:
        """Columns a record from an earlier toolpass lacks."""
        if self._db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'calls'").fetchone() is None:
            return
        have = {r[1] for r in self._db.execute("PRAGMA table_info(calls)").fetchall()}
        for column, kind in (("started", "REAL NOT NULL DEFAULT 0"), ("ended", "REAL")):
            if column not in have:
                _add_column(self._db, "calls", column, kind)

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

    @contextlib.contextmanager
    def _write(self) -> Iterator[tuple[sqlite3.Connection, int]]:
        """One write transaction, holding the next ``rev``."""
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                rev = int(self._db.execute("SELECT v FROM meta WHERE k = 'rev'").fetchone()[0]) + 1
                self._db.execute("UPDATE meta SET v = ? WHERE k = 'rev'", (rev,))
                yield self._db, rev
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            self._db.execute("COMMIT")

    # -- writers ----------------------------------------------------------------

    def record_server(self, client: str, name: str, config: str, transport: str, target: str, wrapped: bool) -> None:
        with self._write() as (db, rev):
            db.execute(
                "INSERT INTO servers (client, name, config, transport, target, wrapped, seen_at, rev) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT (client, config, name) DO UPDATE SET transport = excluded.transport, target = excluded.target,"
                " wrapped = excluded.wrapped, seen_at = excluded.seen_at, rev = excluded.rev",
                (client, name, config, transport, _cut(target), int(wrapped), _now(), rev),
            )

    def forget_servers(self, configs: Sequence[str], keep: Sequence[tuple[str, str, str]]) -> None:
        """Drop the servers listed under ``configs`` that a scan no longer
        found; ``keep`` is the (client, config, name) of those it did."""
        kept = set(keep)
        with self._write() as (db, _):
            for row in db.execute("SELECT client, config, name FROM servers").fetchall():
                listed = any(row["config"] == c or row["config"].startswith(c + " (") for c in configs)
                if listed and (row["client"], row["config"], row["name"]) not in kept:
                    db.execute("DELETE FROM servers WHERE client = ? AND config = ? AND name = ?", (row["client"], row["config"], row["name"]))

    def record_tools(self, client: str, config: str, server: str, tools: Sequence[Mapping[str, Any]]) -> None:
        with self._write() as (db, rev):
            for t in tools:
                name = t.get("name")
                if not isinstance(name, str):
                    continue
                annotations = t.get("annotations") if isinstance(t.get("annotations"), Mapping) else {}
                db.execute(
                    "INSERT INTO tools (client, config, server, name, annotations, rev) VALUES (?, ?, ?, ?, ?, ?)"
                    " ON CONFLICT (client, config, server, name) DO UPDATE SET annotations = excluded.annotations, rev = excluded.rev",
                    (client, config, server, name, json.dumps(annotations, default=repr), rev),
                )

    def start_call(self, client: str, config: str, server: str, session: str, tool: str, arguments: Mapping[str, Any], started: float | None = None) -> int:
        """Record a call that was just sent to the server; returns its id.
        ``started`` is when it was sent (wall clock), now by default."""
        at = started or time.time()
        with self._write() as (db, rev):
            cur = db.execute(
                "INSERT INTO calls (time, client, config, server, session, tool, arguments, outcome, started, rev)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, 'running', ?, ?)",
                (_now(), client, config, server, session, tool, _cut(json.dumps(arguments, default=repr, ensure_ascii=False)), at, rev),
            )
            call_id = int(cur.lastrowid or 0)
        self._private_side_files()
        return call_id

    def finish_call(self, call_id: int, outcome: str, result: str, duration_ms: float, ended: float | None = None) -> None:
        """Record the answer to a call; ``ended`` is when it arrived (wall clock), now by default."""
        with self._write() as (db, rev):
            db.execute(
                "UPDATE calls SET outcome = ?, result = ?, duration_ms = ?, ended = ?, rev = ? WHERE id = ?",
                (outcome, _cut(result), round(duration_ms, 1), ended or time.time(), rev, call_id),
            )
            row = db.execute("SELECT session FROM calls WHERE id = ?", (call_id,)).fetchone()
            if row is not None:
                _place(db, rev, row[0])

    # -- readers ----------------------------------------------------------------

    def record_tool_event(self, session: str, event: Mapping[str, Any], started: float, ended: float) -> None:
        """What a toolpass-secured tool inside a wrapped server did during a
        call: its audit event, with the requests it made to GitHub, Jira, ...
        It is placed under its call (``_OWNER``) as soon as that call has
        finished, now or when its answer is recorded; the call's rev moves so
        the dashboard shows it."""
        tool = str(event.get("tool", ""))
        with self._write() as (db, rev):
            db.execute(
                "INSERT INTO tool_events (session, tool, started, ended, event) VALUES (?, ?, ?, ?, ?)",
                (session, tool, started, ended, _cut_event(event)),
            )
            _place(db, rev, session)

    def prune(self, days: float = 90) -> int:
        """Delete calls older than ``days``; returns how many."""
        cutoff = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() - days * 86400))
        with self._write() as (db, _):
            db.execute("DELETE FROM tool_events WHERE started < ?", (time.time() - days * 86400,))
            return int(db.execute("DELETE FROM calls WHERE time < ?", (cutoff,)).rowcount)

    def calls(self, since: int = 0, limit: int = 500) -> tuple[list[dict[str, Any]], int]:
        """Calls added or changed after ``since`` (oldest first, at most
        ``limit``), and the rev to pass next time. With ``since`` 0, the
        latest ``limit`` calls, so a fresh page does not replay all history."""
        with self._lock:
            # One snapshot: a write committed between the two reads cannot fall in the gap.
            self._db.execute("BEGIN")
            try:
                current = int(self._db.execute("SELECT v FROM meta WHERE k = 'rev'").fetchone()[0])
                if since <= 0:
                    rows = self._db.execute("SELECT * FROM (SELECT * FROM calls ORDER BY rev DESC LIMIT ?) ORDER BY rev", (limit,)).fetchall()
                else:
                    rows = self._db.execute("SELECT * FROM calls WHERE rev > ? ORDER BY rev LIMIT ?", (since, limit)).fetchall()
            finally:
                self._db.execute("COMMIT")
            events = self._events_for(rows)
        out = []
        for r in rows:
            row = dict(r)
            try:
                row["arguments"] = json.loads(row["arguments"])
            except ValueError:
                pass
            row["inside"] = events.get(row["id"], [])
            out.append(row)
        nxt = int(out[-1]["rev"]) if since > 0 and len(out) == limit else current
        return out, nxt

    def _events_for(self, rows: Sequence[sqlite3.Row]) -> dict[int, list[dict[str, Any]]]:
        """The tool events placed under each call. Called with the lock held."""
        out: dict[int, list[dict[str, Any]]] = {}
        ids = [int(r["id"]) for r in rows]
        for chunk in range(0, len(ids), 500):
            part = ids[chunk : chunk + 500]
            marks = ",".join("?" * len(part))
            found = self._db.execute(f"SELECT call_id, event FROM tool_events WHERE call_id IN ({marks}) ORDER BY started, id", part).fetchall()
            for e in found:
                try:
                    out.setdefault(int(e["call_id"]), []).append(json.loads(e["event"]))
                except ValueError:
                    continue
        return out

    def servers(self) -> list[dict[str, Any]]:
        with self._lock:
            servers = [dict(r) for r in self._db.execute("SELECT * FROM servers ORDER BY client, name").fetchall()]
            tools = [dict(r) for r in self._db.execute("SELECT * FROM tools ORDER BY name").fetchall()]
            counts = {
                (r["client"], r["config"], r["server"]): (r["n"], r["last"])
                for r in self._db.execute(
                    "SELECT client, config, server, COUNT(*) AS n, MAX(time) AS last FROM calls GROUP BY client, config, server"
                ).fetchall()
            }
        for s in servers:
            s["wrapped"] = bool(s["wrapped"])
            s["tools"] = [
                {"name": t["name"], "annotations": json.loads(t["annotations"])}
                for t in tools
                if (t["client"], t["config"], t["server"]) == (s["client"], s["config"], s["name"])
            ]
            s["calls"], s["last_call"] = counts.get((s["client"], s["config"], s["name"]), (0, None))
        return servers

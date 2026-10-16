"""The dashboard: every run of a toolpass tool, written by the process that ran
it when TOOLPASS_DASHBOARD is set, read by `toolpass dashboard`."""

from __future__ import annotations

import http.client
import io
import json
import os
import re
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from toolpass import _record, cli
from toolpass._dashboard import Dashboard
from toolpass._store import Store, data_dir

RUN = {
    "time": "2026-10-16T12:00:00.000+00:00",
    "tool": "open_pr",
    "effect": "write",
    "outcome": "refused",
    "user": "sam@acme.dev",
    "reason": "denied: sam has read",
    "actions": [{"integration": "github", "action": "pr.create", "outcome": "refused"}],
}


def test_two_writers_one_file(tmp_path: Path) -> None:
    a, b = Store(tmp_path / "d.db"), Store(tmp_path / "d.db")
    a.record_run("shop", RUN)
    b.record_run("ops", {**RUN, "tool": "ship"})
    rows, nxt = a.runs(0)
    assert [(r["server"], r["tool"]) for r in rows] == [("shop", "open_pr"), ("ops", "ship")]
    assert rows[0]["actions"][0]["action"] == "pr.create" and rows[0]["user"] == "sam@acme.dev"
    assert a.runs(nxt) == ([], nxt)
    a.record_run("shop", {**RUN, "tool": "status"})
    assert [r["tool"] for r in a.runs(nxt)[0]] == ["status"]


def test_prune(tmp_path: Path) -> None:
    s = Store(tmp_path / "d.db")
    s.record_run("shop", RUN, at=time.time() - 100 * 86400)
    s.record_run("shop", RUN)
    assert s.prune(days=90) == 1 and len(s.runs(0)[0]) == 1


def test_a_huge_run_keeps_its_outcome(tmp_path: Path) -> None:
    s = Store(tmp_path / "d.db")
    s.record_run("shop", {**RUN, "arguments": {"body": "x" * 100_000}})
    [row] = s.runs(0)[0]
    assert row["outcome"] == "refused" and "arguments" not in row and "left out" in row["cut"]


@pytest.mark.skipif(os.name == "nt", reason="POSIX modes")
def test_the_record_is_private(tmp_path: Path) -> None:
    s = Store(tmp_path / "new" / "d.db")
    s.record_run("shop", RUN)
    assert stat.S_IMODE(os.stat(tmp_path / "new").st_mode) == 0o700
    for name in ("d.db", "d.db-wal", "d.db-shm"):
        f = tmp_path / "new" / name
        if f.exists():
            assert stat.S_IMODE(os.stat(f).st_mode) == 0o600, name


def test_where_the_record_is(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TOOLPASS_DATA_DIR", str(tmp_path))
    assert data_dir() == tmp_path
    for off in ("", "0", "off"):
        monkeypatch.setenv(_record.ENV, off)
        assert _record.target() is None
    monkeypatch.setenv(_record.ENV, "1")
    assert _record.target() == tmp_path / "dashboard.db"
    monkeypatch.setenv(_record.ENV, str(tmp_path / "x.db"))
    assert _record.target() == tmp_path / "x.db"


def test_a_toolkit_writes_its_runs_when_asked(tmp_path: Path) -> None:
    """A process with TOOLPASS_DASHBOARD set writes each run, refused ones too; without it, nothing."""
    script = (
        "from toolpass import Session, Toolkit\n"
        "tk = Toolkit(audit=None)\n"
        "@tk.tool(effect='read')\n"
        "def lookup(key: str) -> str:\n"
        "    return 'found ' + key\n"
        "with Session('dana@acme.dev').active():\n"
        "    lookup(key='SHOP-1')\n"
    )
    db = tmp_path / "d.db"
    env = {**os.environ, _record.ENV: str(db), "TOOLPASS_SERVER_NAME": "shop"}
    subprocess.run([sys.executable, "-c", script], env=env, check=True, timeout=60)
    [row] = Store(db).runs(0)[0]
    assert (row["server"], row["tool"], row["user"], row["outcome"]) == ("shop", "lookup", "dana@acme.dev", "ran")
    env.pop(_record.ENV)
    subprocess.run([sys.executable, "-c", script], env=env, check=True, timeout=60)
    assert len(Store(db).runs(0)[0]) == 1


def _get(url: str, path: str, host: str | None = None) -> tuple[int, Any]:
    m = re.fullmatch(r"http://([^:]+):(\d+)(/t/[^/]+/)", url)
    assert m
    conn = http.client.HTTPConnection(m.group(1), int(m.group(2)), timeout=5)
    conn.request("GET", path if path.startswith("/") else m.group(3) + path, headers={"Host": host} if host else {})
    r = conn.getresponse()
    body = r.read()
    conn.close()
    return r.status, (json.loads(body) if r.getheader("Content-Type", "").startswith("application/json") else body)


def test_dashboard_shows_runs(tmp_path: Path) -> None:
    store = Store(tmp_path / "d.db")
    store.record_run("shop", RUN)
    with Dashboard(store) as page:
        status, d = _get(page.url, "events?since=0")
        assert status == 200 and d["kind"] == "dashboard" and [(e["server"], e["tool"]) for e in d["events"]] == [("shop", "open_pr")]
        store.record_run("shop", {**RUN, "tool": "ship"})
        _, d2 = _get(page.url, f"events?since={d['next']}")
        assert [e["tool"] for e in d2["events"]] == ["ship"]
        assert _get(page.url, "servers")[0] == 404
        assert _get(page.url, "/t/wrong/events")[0] == 404
        assert _get(page.url, "events", host="evil.example")[0] == 403


def test_dashboard_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TOOLPASS_DATA_DIR", str(tmp_path))
    monkeypatch.delenv(_record.ENV, raising=False)
    Store(tmp_path / "dashboard.db").record_run("shop", RUN)
    stop = threading.Event()
    out, err = io.StringIO(), io.StringIO()
    result: list[int] = []
    t = threading.Thread(target=lambda: result.append(cli.dashboard(["-no-wrap"], out, err, stop=stop)))
    t.start()
    for _ in range(100):
        if "toolpass dashboard: http://127.0.0.1:" in out.getvalue():
            break
        threading.Event().wait(0.05)
    url = re.search(r"(http://\S+)", out.getvalue())
    assert url, out.getvalue() + err.getvalue()
    assert [e["tool"] for e in _get(url.group(1), "events?since=0")[1]["events"]] == ["open_pr"]
    stop.set()
    t.join(10)
    assert result == [0]
    assert cli.dashboard(["-listen", "0.0.0.0:0"], io.StringIO(), err) == 2 and "loopback" in err.getvalue()

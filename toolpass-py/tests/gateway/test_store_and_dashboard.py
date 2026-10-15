"""The store under several writers, and the dashboard over it."""

from __future__ import annotations

import http.client
import io
import json
import re
import threading
from pathlib import Path
from typing import Any

import pytest

from toolpass import cli
from toolpass._dashboard import Dashboard
from toolpass._store import Store, data_dir


def test_two_writers_one_file(tmp_path: Path) -> None:
    path = tmp_path / "g.db"
    a, b = Store(path), Store(path)  # two connections, as two wrapped servers have

    def write(store: Store, server: str) -> None:
        for i in range(40):
            call = store.start_call("c", server, "s", "t", {"i": i})
            store.finish_call(call, "ok", str(i), 1.0)

    threads = [threading.Thread(target=write, args=(a, "one")), threading.Thread(target=write, args=(b, "two"))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    rows, nxt = Store(path).calls(0, limit=1000)
    assert len(rows) == 80 and all(r["outcome"] == "ok" for r in rows)
    assert len({r["rev"] for r in rows}) == 80, "every change has its own rev"
    assert Store(path).calls(nxt)[0] == []


def test_calls_page_through_by_rev(tmp_path: Path) -> None:
    s = Store(tmp_path / "g.db")
    ids = [s.start_call("c", "srv", "s", "t", {}) for _ in range(5)]
    rows, nxt = s.calls(0, limit=3)
    assert len(rows) == 3
    rows2, nxt2 = s.calls(nxt, limit=3)
    assert [r["id"] for r in rows + rows2] == ids
    s.finish_call(ids[0], "error", "boom", 2.0)
    changed, _ = s.calls(nxt2)
    assert [(r["id"], r["outcome"]) for r in changed] == [(ids[0], "error")], "a finished call shows up again"


def test_data_dir_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TOOLPASS_DATA_DIR", str(tmp_path))
    assert data_dir() == tmp_path


def _get(url: str, path: str, host: str | None = None) -> tuple[int, Any]:
    m = re.fullmatch(r"http://([^:]+):(\d+)(/t/[^/]+/)", url)
    assert m
    conn = http.client.HTTPConnection(m.group(1), int(m.group(2)), timeout=5)
    conn.request("GET", path if path.startswith("/") else m.group(3) + path, headers={"Host": host} if host else {})
    r = conn.getresponse()
    body = r.read()
    conn.close()
    return r.status, (json.loads(body) if r.getheader("Content-Type", "").startswith("application/json") else body)


def test_dashboard_shows_calls_and_servers(tmp_path: Path) -> None:
    store = Store(tmp_path / "g.db")
    store.record_server("claude-desktop", "github", "/x.json", "stdio", "npx gh", True)
    store.record_tools("claude-desktop", "github", [{"name": "delete_repo", "annotations": {"destructiveHint": True}}])
    call = store.start_call("claude-desktop", "github", "s1", "delete_repo", {"repo": "acme/x"})
    with Dashboard(store) as page:
        status, d = _get(page.url, "events?since=0")
        assert status == 200 and d["kind"] == "gateway" and d["events"][0]["outcome"] == "running"
        store.finish_call(call, "ok", "deleted", 5.0)
        _, d2 = _get(page.url, f"events?since={d['next']}")
        assert [(e["id"], e["outcome"]) for e in d2["events"]] == [(call, "ok")]
        _, s = _get(page.url, "servers")
        assert s["servers"][0]["name"] == "github" and s["servers"][0]["calls"] == 1
        assert s["servers"][0]["tools"] == [{"name": "delete_repo", "annotations": {"destructiveHint": True}}]
        assert _get(page.url, "/t/wrong/servers")[0] == 404
        assert _get(page.url, "servers", host="evil.example")[0] == 403
        status, html = _get(page.url, "")
        assert status == 200 and b"Servers" in html


def test_the_toolkit_page_has_no_servers_route() -> None:
    from toolpass import Toolkit, watch

    with watch(Toolkit(audit=None)) as page:
        assert _get(page.url, "servers")[0] == 404


def test_dashboard_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from toolpass import _gateway

    monkeypatch.setenv("TOOLPASS_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(_gateway, "client_files", lambda home=None: [])
    stop = threading.Event()
    out, err = io.StringIO(), io.StringIO()
    result: list[int] = []
    t = threading.Thread(target=lambda: result.append(cli.dashboard([], out, err, stop=stop)))
    t.start()
    for _ in range(100):
        if "toolpass dashboard: http://127.0.0.1:" in out.getvalue():
            break
        threading.Event().wait(0.05)
    url = re.search(r"(http://\S+)", out.getvalue())
    assert url, out.getvalue() + err.getvalue()
    assert _get(url.group(1), "servers") == (200, {"servers": []})
    stop.set()
    t.join(10)
    assert result == [0] and "0 MCP server(s) found" in out.getvalue()
    assert cli.dashboard(["-listen", "0.0.0.0:0"], io.StringIO(), err) == 2 and "loopback" in err.getvalue()

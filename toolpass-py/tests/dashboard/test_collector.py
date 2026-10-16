"""The toolpass server collects the runs of every agent's secured tools
(POST /runs) and shows them at /dashboard/; a toolkit whose permissions are
remote reports there with nothing else configured."""

from __future__ import annotations

import http.client
import json
import os
import subprocess
import sys
import time
from collections.abc import Iterator
from typing import Any

import pytest

from toolpass import _record
from toolpass._runs import RunLog, decode_runs
from toolpass.core import secret
from toolpass.core.engine import Decision
from toolpass.server import Server

KEY = "collector-key"
RUN = {
    "time": "2026-10-16T12:00:00.000+00:00",
    "tool": "open_pr",
    "effect": "write",
    "outcome": "refused",
    "user": "sam@acme.dev",
    "reason": "denied: sam has read",
    "actions": [{"integration": "github", "action": "pr.create", "outcome": "refused"}],
}


class _Deny:
    def check(self, ctx: Any, req: Any) -> Any:
        from toolpass.core.engine import Result

        return Result(200, Decision("deny", "denied: test", 200))


class Running:
    def __init__(self, runs: RunLog | None = None) -> None:
        self.server = Server(_Deny(), secret.literal(KEY), runs=runs)
        self.host, self.port = self.server.listen("127.0.0.1:0")
        self.server.serve_in_thread()
        self.url = f"http://{self.host}:{self.port}"

    def request(self, method: str, path: str, body: bytes | None = None, headers: dict[str, str] | None = None) -> tuple[int, dict[str, str], bytes]:
        conn = http.client.HTTPConnection(self.host, self.port, timeout=5)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            res = conn.getresponse()
            return res.status, {k.lower(): v for k, v in res.getheaders()}, res.read()
        finally:
            conn.close()

    def events(self, since: int = 0) -> dict[str, Any]:
        status, _, body = self.request("GET", f"/dashboard/events?since={since}", headers={"Authorization": "Bearer " + KEY})
        assert status == 200, body
        return dict(json.loads(body))


@pytest.fixture
def running() -> Iterator[Running]:
    r = Running()
    yield r
    r.server.shutdown()


def _auth(key: str = KEY) -> dict[str, str]:
    return {"Authorization": "Bearer " + key, "Content-Type": "application/json"}


def test_runs_are_collected_and_served(running: Running) -> None:
    body = json.dumps({"server": "shop", "runs": [RUN, {**RUN, "tool": "ship", "outcome": "ran"}]}).encode()
    status, _, out = running.request("POST", "/runs", body, _auth())
    assert (status, json.loads(out)) == (202, {"accepted": 2})
    d = running.events()
    assert d["kind"] == "dashboard"
    assert [(e["server"], e["tool"], e["outcome"]) for e in d["events"]] == [("shop", "open_pr", "refused"), ("shop", "ship", "ran")]
    assert d["events"][0]["actions"][0]["action"] == "pr.create" and d["events"][0]["user"] == "sam@acme.dev"
    assert running.events(d["next"])["events"] == [], "nothing new after the last seq"
    running.request("POST", "/runs", json.dumps({"server": "ops", "runs": [RUN]}).encode(), _auth())
    assert [e["server"] for e in running.events(d["next"])["events"]] == ["ops"]


def test_runs_need_the_api_key(running: Running) -> None:
    body = json.dumps({"server": "shop", "runs": [RUN]}).encode()
    for headers in ({}, _auth("wrong")):
        status, h, _ = running.request("POST", "/runs", body, headers)
        assert status == 401 and "bearer" in h.get("www-authenticate", "").lower()
    assert running.request("GET", "/runs", headers=_auth())[0] == 405
    assert len(running.server.runs) == 0


@pytest.mark.parametrize(
    ("body", "why"),
    [
        (b"[]", "object"),
        (b'{"server": "", "runs": [{}]}', '"server"'),
        (b'{"server": "shop", "runs": []}', '"runs"'),
        (b'{"server": "shop", "runs": [{"tool": "x"}]}', "runs[0]"),
        (b"{nope", "invalid JSON"),
    ],
)
def test_bad_batches_are_refused(running: Running, body: bytes, why: str) -> None:
    status, _, out = running.request("POST", "/runs", body, _auth())
    assert status == 400 and why in json.loads(out)["error"]


def test_a_batch_may_be_larger_than_a_check(running: Running) -> None:
    big = {**RUN, "arguments": {"body": "x" * 100_000}}
    status, _, _ = running.request("POST", "/runs", json.dumps({"server": "shop", "runs": [big]}).encode(), _auth())
    assert status == 202
    [row] = running.events()["events"]
    assert "arguments" not in row and "left out" in row["cut"], "a huge run keeps its outcome and drops the rest"
    too_big = json.dumps({"server": "shop", "runs": [{**RUN, "arguments": {"b": "x" * 1_100_000}}]}).encode()
    try:
        status = running.request("POST", "/runs", too_big, _auth())[0]
    except (BrokenPipeError, ConnectionResetError):
        status = 413  # the server answered and closed before reading the rest of it
    assert status == 413
    assert len(running.server.runs) == 1


def test_the_log_is_bounded() -> None:
    log = RunLog(keep=3)
    for i in range(5):
        log.add("shop", {**RUN, "tool": f"t{i}"})
    rows, nxt = log.runs(0)
    assert [r["tool"] for r in rows] == ["t2", "t3", "t4"] and nxt == 5
    assert decode_runs(json.dumps({"server": " shop ", "runs": [RUN]}).encode())[0] == "shop"


def test_the_page_is_public_and_the_runs_are_not(running: Running) -> None:
    status, h, body = running.request("GET", "/dashboard/")
    assert status == 200 and h["content-type"].startswith("text/html") and b"watch.js" in body
    assert "default-src 'none'" in h["content-security-policy"]
    assert running.request("GET", "/dashboard/watch.js")[0] == 200
    assert running.request("GET", "/dashboard/watch.css")[0] == 200
    status, h, _ = running.request("GET", "/dashboard")
    assert (status, h["location"]) == (308, "/dashboard/")
    status, h, _ = running.request("GET", "/dashboard/events")
    assert status == 401 and "bearer" in h["www-authenticate"].lower()
    assert running.request("GET", "/dashboard/nope")[0] == 404
    assert running.request("POST", "/dashboard/events", headers=_auth())[0] == 405


def test_a_remote_toolkit_reports_its_runs(running: Running, monkeypatch: pytest.MonkeyPatch) -> None:
    """A process whose toolkit asks a toolpass server also reports each run
    there: refused runs too, with the requests under them; TOOLPASS_DASHBOARD=off stops it."""
    script = (
        "from toolpass import Session, Toolkit, Toolpass\n"
        "tp = Toolpass.remote()\n"
        "tk = Toolkit(audit=None, permissions=tp, connections=[])\n"
        "@tk.tool(effect='read')\n"
        "def lookup(key: str) -> str:\n"
        "    return 'found ' + key\n"
        "with Session('dana@acme.dev').active():\n"
        "    lookup(key='SHOP-1')\n"
        "    lookup(key='SHOP-2')\n"
    )
    env = {**os.environ, "TOOLPASS_URL": running.url, "TOOLPASS_API_KEY": KEY, "TOOLPASS_SERVER_NAME": "shop"}
    env.pop(_record.ENV, None)
    subprocess.run([sys.executable, "-c", script], env=env, check=True, timeout=60)
    rows = running.events()["events"]
    assert [(r["server"], r["tool"], r["user"], r["outcome"]) for r in rows] == [("shop", "lookup", "dana@acme.dev", "ran")] * 2
    assert rows[0]["arguments"] == {"key": "SHOP-1"} and "at" in rows[0]
    subprocess.run([sys.executable, "-c", script], env={**env, _record.ENV: "off"}, check=True, timeout=60)
    assert len(running.events()["events"]) == 2, "off: nothing more reported"


def test_an_in_process_toolkit_reports_when_told_where(running: Running) -> None:
    script = (
        "from toolpass import Session, Toolkit\n"
        "tk = Toolkit(audit=None)\n"
        "@tk.tool(effect='read')\n"
        "def lookup(key: str) -> str:\n"
        "    return 'found ' + key\n"
        "with Session('dana@acme.dev').active():\n"
        "    lookup(key='SHOP-1')\n"
    )
    env = {**os.environ, "TOOLPASS_URL": running.url, "TOOLPASS_API_KEY": KEY}
    env.pop(_record.ENV, None)
    subprocess.run([sys.executable, "-c", script], env=env, check=True, timeout=60)
    [row] = running.events()["events"]
    assert row["tool"] == "lookup" and row["server"] == "python", "no TOOLPASS_SERVER_NAME: named after the script"
    env.pop("TOOLPASS_URL")
    subprocess.run([sys.executable, "-c", script], env=env, check=True, timeout=60)
    assert len(running.events()["events"]) == 1, "no server named: nothing reported, nothing written anywhere"


def test_an_unreachable_server_never_delays_a_tool() -> None:
    script = (
        "import time\n"
        "from toolpass import Session, Toolkit\n"
        "tk = Toolkit(audit=None)\n"
        "@tk.tool(effect='read')\n"
        "def lookup(key: str) -> str:\n"
        "    return 'found ' + key\n"
        "t0 = time.perf_counter()\n"
        "with Session('dana@acme.dev').active():\n"
        "    for i in range(20):\n"
        "        lookup(key=str(i))\n"
        "print(time.perf_counter() - t0)\n"
        "from toolpass import _record\n"
        "_record._sender.close()  # drain now, so what it said is on stderr before the process ends\n"
    )
    env = {**os.environ, "TOOLPASS_URL": "http://127.0.0.1:9", "TOOLPASS_API_KEY": "k"}
    env.pop(_record.ENV, None)
    t0 = time.perf_counter()
    done = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    assert float(done.stdout.strip()) < 2.0, "the tools ran without waiting on the server"
    assert time.perf_counter() - t0 < 30
    assert "did not take a batch" in done.stderr and done.stderr.count("toolpass:") == 1, "said once, on stderr"


def test_target() -> None:
    env_before = {k: os.environ.get(k) for k in (_record.ENV, "TOOLPASS_URL", "TOOLPASS_API_KEY")}
    try:
        for k in env_before:
            os.environ.pop(k, None)
        assert _record.target() is None

        class Remote:
            server = ("https://tp.example", "k1")

        assert _record.target(Remote()) == ("https://tp.example", "k1")
        os.environ["TOOLPASS_URL"], os.environ["TOOLPASS_API_KEY"] = "https://env.example/", "k2"
        assert _record.target() == ("https://env.example", "k2")
        os.environ[_record.ENV] = "https://other.example"
        assert _record.target(Remote()) == ("https://other.example", "k2"), "an explicit target wins"
        os.environ[_record.ENV] = "off"
        assert _record.target(Remote()) is None
    finally:
        for k, v in env_before.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

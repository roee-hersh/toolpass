"""The demo in one command: two people use the ``shop`` MCP server, built with
toolpass, and the toolpass server's dashboard shows every tool run: who it
ran for, and each request it made to GitHub, Jira, Slack, Linear and
Kubernetes, allowed or refused by the system itself.

    pip install toolpass            # or, from toolpass-py: pip install -e .
    python examples/demo/run.py     # then open the address it prints

Dana, a developer, opens a pull request, ships to staging, is refused prod,
and files a bug. Sam, who joined support this week, can look at everything
and change nothing: the same tools stop at the first request Sam may not
make, and the dashboard shows which one and why.

Everything runs on this machine, the way a team would run it: a toolpass
server (``toolpass serve``) holds the connections, answers the shop server's
permission questions and collects its runs; the five systems are stand-ins
(``fake_*.py``). Nothing is written outside a temporary folder.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import IO, Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import world  # noqa: E402
from shop_server import Systems  # noqa: E402

TOUR: list[tuple[str, str, dict[str, str]]] = [
    (world.DANA, "status", {"issue": "SHOP-12"}),
    (world.DANA, "open_pr", {"repo": "acme/shop", "branch": "fix-safari-checkout", "title": "Fix checkout on Safari", "issue": "SHOP-12"}),
    (world.DANA, "ship", {"service": "shop-web", "version": "1.5.0", "env": "staging"}),
    (world.DANA, "ship", {"service": "shop-web", "version": "1.5.0", "env": "prod"}),
    (world.DANA, "report_bug", {"title": "Coupon field accepts expired codes", "details": "Seen on staging with code SUMMER23."}),
    (world.SAM, "status", {"issue": "SHOP-12"}),
    (world.SAM, "open_pr", {"repo": "acme/shop", "branch": "typo", "title": "Fix a typo", "issue": "SHOP-14"}),
    (world.SAM, "ship", {"service": "shop-web", "version": "1.5.1", "env": "staging"}),
    (world.SAM, "report_bug", {"title": "Customer cannot reset password", "details": "Ticket #4412."}),
]


def toolpass(*args: str) -> list[str]:
    return [sys.executable, "-m", "toolpass.cli", *args]


class Collector:
    """A toolpass server for the demo: the engine the shop server asks about
    each request, and the dashboard that collects and shows its runs."""

    def __init__(self, connections: list[dict[str, Any]], listen: str = "127.0.0.1:0") -> None:
        host, _, port = listen.rpartition(":")
        host = host or "127.0.0.1"
        if port in ("", "0"):
            with socket.socket() as s:  # a free port, picked here so the address is known before the server starts
                s.bind((host, 0))
                port = str(s.getsockname()[1])
        self.url = f"http://{host}:{port}"
        self.key = secrets.token_urlsafe(18)
        self._dir = tempfile.TemporaryDirectory(prefix="toolpass-demo-")
        config = Path(self._dir.name) / "toolpass.yaml"
        doc = {"api_key": "env:TOOLPASS_API_KEY", "listen": f"{host}:{port}", "decision_log": "none", "connections": connections}
        config.write_text(json.dumps(doc))  # JSON is YAML
        self.proc = subprocess.Popen(
            toolpass("serve", "-config", str(config), "-log-level", "warn"), env={**os.environ, "TOOLPASS_API_KEY": self.key}, stderr=subprocess.PIPE, text=True
        )
        for _ in range(100):
            if self.proc.poll() is not None:
                raise RuntimeError(f"toolpass serve stopped: {self.proc.stderr.read() if self.proc.stderr else ''}")
            try:
                with urllib.request.urlopen(self.url + "/healthz", timeout=1):
                    break
            except OSError:
                time.sleep(0.1)
        else:
            raise RuntimeError("toolpass serve did not come up")

    @property
    def dashboard(self) -> str:
        """The page, with the key in the fragment: the browser keeps it, the server never sees it."""
        return f"{self.url}/dashboard/#key={self.key}"

    def env(self) -> dict[str, str]:
        """What a process needs to ask this server and report to it."""
        return {"TOOLPASS_URL": self.url, "TOOLPASS_API_KEY": self.key}

    def runs(self) -> list[dict[str, Any]]:
        req = urllib.request.Request(self.url + "/dashboard/events?since=0", headers={"Authorization": "Bearer " + self.key})
        with urllib.request.urlopen(req, timeout=5) as resp:
            return list(json.load(resp)["events"])

    def close(self) -> None:
        self.proc.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            self.proc.wait(timeout=10)
        if self.proc.poll() is None:
            self.proc.kill()
        self._dir.cleanup()


class Client:
    """An MCP client over stdio, the way Claude Desktop or Claude Code starts the server."""

    def __init__(self, user: str, env: dict[str, str]) -> None:
        cmd = [sys.executable, str(HERE / "shop_server.py")]
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, env={**env, "DEMO_USER": user})
        self.next_id = 0
        self.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "demo", "version": "1"}})
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.request("tools/list", {})

    def _send(self, msg: dict[str, Any]) -> None:
        stdin: IO[str] = self.proc.stdin  # type: ignore[assignment]
        stdin.write(json.dumps(msg) + "\n")
        stdin.flush()

    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.next_id += 1
        self._send({"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params})
        stdout: IO[str] = self.proc.stdout  # type: ignore[assignment]
        while True:
            line = stdout.readline()
            if not line:
                raise RuntimeError("the shop server stopped")
            msg = json.loads(line)
            if msg.get("id") == self.next_id:
                return dict(msg.get("result") or {"error": msg.get("error")})

    def close(self) -> None:
        if self.proc.stdin:
            self.proc.stdin.close()
        self.proc.wait(timeout=30)


def tour(env: dict[str, str], say: Any = print) -> list[tuple[str, str, bool]]:
    """Run every call in TOUR; (user, tool, ok) for each."""
    out: list[tuple[str, str, bool]] = []
    clients: dict[str, Client] = {}
    try:
        for user, tool, args in TOUR:
            client = clients.get(user) or clients.setdefault(user, Client(user, env))
            result = client.request("tools/call", {"name": tool, "arguments": args})
            ok = not result.get("isError")
            text = " ".join(c.get("text", "") for c in result.get("content", []))
            say(f"{'ok     ' if ok else 'REFUSED'} {user:15} {tool:11} {text.splitlines()[0] if text else ''}")
            out.append((user, tool, ok))
    finally:
        for c in clients.values():
            c.close()
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-listen", default="127.0.0.1:0", help="the toolpass server's address (default: a free port on 127.0.0.1)")
    parser.add_argument("-no-dashboard", action="store_true", help="run the calls and exit")
    parser.add_argument("-open", action="store_true", help="open the dashboard in a browser")
    a = parser.parse_args()
    systems = Systems()
    collector = None
    try:
        collector = Collector(systems.connections, a.listen)
        env = {**os.environ, **collector.env(), "DEMO_SYSTEMS": systems.shared()}
        print("Two people use the shop MCP server, built with toolpass; a toolpass server decides each request:\n")
        tour(env)
        if a.no_dashboard:
            return
        print(f"\nThe dashboard shows each run and every request the tool made: {collector.dashboard}\nCtrl-C to stop.\n")
        if a.open:
            from toolpass._watch import open_in_browser

            open_in_browser(collector.dashboard)
        with contextlib.suppress(KeyboardInterrupt):
            collector.proc.wait()
    finally:
        if collector is not None:
            collector.close()
        systems.close()


if __name__ == "__main__":
    main()

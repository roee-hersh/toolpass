"""The demo in one command: two people use the ``shop`` MCP server, built with
toolpass, and the dashboard shows every tool run: who it ran for, and each
request it made to GitHub, Jira, Slack, Linear and Kubernetes, allowed or
refused by the system itself.

    pip install toolpass            # or, from toolpass-py: pip install -e .
    python examples/demo/run.py     # then open the address it prints

Dana, a developer, opens a pull request, ships to staging, is refused prod,
and files a bug. Sam, who joined support this week, can look at everything
and change nothing: the same tools stop at the first request Sam may not
make, and the dashboard shows which one and why.

Everything runs on this machine: the five systems are stand-ins
(``fake_*.py``), and the record is kept in a temporary folder, not your own.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import subprocess
import sys
import tempfile
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
    parser.add_argument("-listen", default="127.0.0.1:0", help="the dashboard's address (default: a free port on 127.0.0.1)")
    parser.add_argument("-no-dashboard", action="store_true", help="run the calls and exit")
    parser.add_argument("-open", action="store_true", help="open the dashboard in a browser")
    a = parser.parse_args()
    systems = Systems()
    try:
        with tempfile.TemporaryDirectory(prefix="toolpass-demo-") as data:
            env = {**os.environ, "TOOLPASS_DATA_DIR": data, "TOOLPASS_DASHBOARD": "1", "DEMO_SYSTEMS": systems.shared()}
            print("Two people use the shop MCP server, built with toolpass:\n")
            tour(env)
            if a.no_dashboard:
                return
            print("\nThe dashboard shows each run and every request the tool made. Ctrl-C to stop.\n")
            cmd = toolpass("dashboard", "-listen", a.listen, *(["-open"] if a.open else []))
            with contextlib.suppress(KeyboardInterrupt):
                subprocess.run(cmd, env=env, check=False)
    finally:
        systems.close()


if __name__ == "__main__":
    main()

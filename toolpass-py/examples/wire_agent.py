"""Actions observed at the wire: a tool with no declarations, held to what it does.

The tool below opens a pull request with nothing but standard-library HTTP
calls (any SDK works the same way: PyGithub, slack_sdk, boto3, ...). It
declares no effect, no scope and no authorizer. toolpass watches the
requests its body makes, maps them to GitHub's actions (`repo.read`,
`pr.create`), asks the permission engine whether the session's user may do
each one, and refuses a request before it is sent. Run it from the
toolpass-py directory, with no network and no model:

    pip install -e .
    python examples/wire_agent.py

A fake GitHub answers on 127.0.0.1; a stand-in permission engine says that
admin@example.com may open pull requests and dana@example.com may not.
"""

from __future__ import annotations

import json
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from typing import Any

from toolpass import AuditEvent, Session, Toolkit, ToolRefused, as_agent
from toolpass.core.integration import Settings
from toolpass.integrations.github import INTEGRATION as GITHUB


@dataclass
class Answer:
    decision: str
    reason: str = ""

    @property
    def allowed(self) -> bool:
        return self.decision == "allow"


class StandInEngine:
    """What Toolpass.from_config("toolpass.yaml") would be, without a GitHub App."""

    def __init__(self, github_url: str) -> None:
        self.github_url = github_url

    def check(self, user: str, connection: str, action: str, resource: str, groups: Any = None, *, fresh: bool = False) -> Answer:
        if action == "repo.read" or user == "admin@example.com":
            return Answer("allow", f"{user} may {action} on {resource}")
        return Answer("deny", f"{user} may not {action} on {resource}")

    def _describe(self) -> list[tuple[str, Any, Any]]:
        return [("github-acme", GITHUB, Settings("github-acme", "github", {"url": self.github_url, "organization": "acme"}))]


class FakeGitHub(BaseHTTPRequestHandler):
    """Enough of GitHub Enterprise Server's REST API for the tool below."""

    seen: list[str] = []

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def _answer(self, status: int, body: dict[str, Any]) -> None:
        FakeGitHub.seen.append(f"{self.command} {self.path}")
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        if self.path == "/api/v3/repos/acme/gitops/contents/deploy.yaml":
            return self._answer(200, {"content": "cmVwbGljYXM6IDI=", "encoding": "base64"})
        return self._answer(404, {"message": "not found"})

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if self.path == "/api/v3/repos/acme/gitops/pulls":
            return self._answer(201, {"number": 482})
        return self._answer(404, {"message": "not found"})


def http(method: str, url: str, body: dict[str, Any] | None = None) -> Any:
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body else None, method=method, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read())


def main(say: Callable[[str], None] = print) -> list[tuple[str, str]]:
    """Run the session; returns (step, outcome) pairs for the tests."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeGitHub)
    Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    events: list[AuditEvent] = []
    tools = Toolkit(permissions=StandInEngine(base), audit=events.append)

    @tools.tool()  # nothing declared: the actions are observed at the wire
    def propose_replicas(repo: str, replicas: int) -> str:
        """Open a pull request setting a deployment's replica count."""
        with as_agent():  # the agent may read the file on its own authority
            current = http("GET", f"{base}/api/v3/repos/{repo}/contents/deploy.yaml")
        pr = http("POST", f"{base}/api/v3/repos/{repo}/pulls", {"title": f"replicas: {replicas}", "head": "scale", "base": "main", "body": str(current)})
        return f"opened PR #{pr['number']} in {repo}"

    outcomes: list[tuple[str, str]] = []

    def step(label: str, fn: Callable[..., Any], **kwargs: Any) -> None:
        try:
            result = fn(**kwargs)
            outcome = "ran"
            say(f"[ran]     {label}\n          -> {result}")
        except ToolRefused as e:
            outcome = e.code
            say(f"[refused] {label}\n          -> {e.code}: {e.reason}")
        actions = ", ".join(f"{a.get('action') or a['method'] + ' ' + a['path']} {a['outcome']}" for a in (events[-1].actions or []))
        say(f"          actions: {actions}")
        outcomes.append((label, outcome))

    say("== dana@example.com asks: 'scale gitops to 3 replicas' ==\n")
    with Session("dana@example.com").active():
        step("propose_replicas(acme/gitops, 3) as dana", propose_replicas, repo="acme/gitops", replicas=3)
    say("\n== admin@example.com asks the same ==\n")
    with Session("admin@example.com").active():
        step("propose_replicas(acme/gitops, 3) as admin", propose_replicas, repo="acme/gitops", replicas=3)
    say(f"\nrequests GitHub received: {FakeGitHub.seen}")
    server.shutdown()
    return outcomes


if __name__ == "__main__":
    main()

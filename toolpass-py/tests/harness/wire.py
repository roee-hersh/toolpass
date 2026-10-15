"""A stand-in permission engine and small HTTP helpers for the tests of actions
observed at the wire (tests/toolkit/test_wire*.py, tests/sdks, tests/frameworks).

    engine = Engine(gh.url)                      # answers by (user, action), records every question
    engine.allow[("dana@example.com", "repo.read")] = True
    tools = Toolkit(permissions=engine)
"""

from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass
from typing import Any

from tests import harness
from toolpass.core.integration import Settings
from toolpass.integrations.github import INTEGRATION as GITHUB
from toolpass.integrations.slack import INTEGRATION as SLACK

__all__ = ["Answer", "Engine", "http", "requests_seen"]


@dataclass
class Answer:
    decision: str
    reason: str = ""

    @property
    def allowed(self) -> bool:
        return self.decision == "allow"


class Engine:
    """Stands in for Toolpass: answers by (user, action) and records questions.

    ``github_url`` is a GitHub Enterprise Server (the API under ``/api/v3``),
    the connection ``github-acme``; ``slack_url``, when given, is ``slack-main``."""

    def __init__(self, github_url: str, slack_url: str | None = None) -> None:
        self.github_url = github_url
        self.slack_url = slack_url
        self.asked: list[tuple[str, str, str, str]] = []
        self.allow: dict[tuple[str, str], bool] = {}

    def check(self, user: str, connection: str, action: str, resource: str, groups: Any = None, *, fresh: bool = False) -> Answer:
        self.asked.append((user, connection, action, resource))
        ok = self.allow.get((user, action), False)
        return Answer("allow" if ok else "deny", "allowed" if ok else f"{user} lacks {action}")

    def _describe(self) -> list[tuple[str, Any, Any]]:
        out = [("github-acme", GITHUB, Settings("github-acme", "github", {"url": self.github_url, "organization": "acme"}))]
        if self.slack_url:
            out.append(("slack-main", SLACK, Settings("slack-main", "slack", {"url": self.slack_url})))
        return out


def http(method: str, url: str, body: dict[str, Any] | None = None) -> Any:
    """One JSON request with the standard library, the way a tool body without an SDK makes it."""
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None, method=method, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read() or b"null")


def requests_seen(srv: harness.Server) -> list[str]:
    """``"METHOD /path"`` for every request the fake received, in order."""
    return [f"{c.method} {c.path}" for c in srv.calls()]

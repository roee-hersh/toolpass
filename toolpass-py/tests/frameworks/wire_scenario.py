"""One scenario every framework test drives through its real agent loop: a
secured tool that declares nothing, whose body talks to a fake GitHub, and
whose pull request is refused at the wire for a user who may not open one.

    gh = wire_github()            # the fake, as a context manager
    engine = wire_engine(gh)      # dana may read, admin may read and open PRs
    tools = Toolkit(audit=None, permissions=engine, on_refuse=str)  # on_refuse where the framework hides exceptions

    @tools.tool()
    def propose_fix(repo: str) -> str: ...   # see propose_body

Under ``Session(DANA).active()`` the body's GET goes out and its POST is
refused before it is sent: the model reads a refusal naming ``pr.create``;
``requests_seen(gh)`` is ``["GET /api/v3/repos/acme/x"]``. Under
``Session(ADMIN).active()`` the tool returns ``"opened #7 in acme/x"`` and
the POST reaches the fake.
"""

from __future__ import annotations

import json
import urllib.request
from typing import Any

from tests import harness
from tests.toolkit.test_wire import Engine, requests_seen

ADMIN = "admin@example.com"
DANA = "dana@example.com"
REPO = "acme/x"
REFUSAL_WORDS = ("pr.create", "dana@example.com")  # both appear in the refusal the model reads

__all__ = ["ADMIN", "DANA", "REFUSAL_WORDS", "REPO", "Engine", "propose_body", "requests_seen", "wire_engine", "wire_github"]


def wire_github() -> harness.Server:
    """A fake GitHub Enterprise Server answering the two requests the tool makes. Close it when done."""
    srv = harness.Server(tls=False)
    srv.json("GET", "/api/v3/repos/acme/x", 200, {"full_name": "acme/x"})
    srv.json("POST", "/api/v3/repos/acme/x/pulls", 201, {"number": 7})
    return srv


def wire_engine(gh: harness.Server) -> Engine:
    e = Engine(gh.url)
    e.allow[(DANA, "repo.read")] = True
    e.allow[(ADMIN, "repo.read")] = True
    e.allow[(ADMIN, "pr.create")] = True
    return e


def propose_body(gh: harness.Server, repo: str) -> str:
    """What the secured tool's body does: a read, then a pull request, with the standard library."""
    _http("GET", f"{gh.url}/api/v3/repos/{repo}")
    pr = _http("POST", f"{gh.url}/api/v3/repos/{repo}/pulls", {"title": "fix", "head": "fix", "base": "main"})
    return f"opened #{pr['number']} in {repo}"


def _http(method: str, url: str, body: dict[str, Any] | None = None) -> Any:
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None, method=method, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read() or b"null")

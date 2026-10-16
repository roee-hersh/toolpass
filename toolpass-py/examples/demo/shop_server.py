"""The ``shop`` MCP server: four tools a software team would write, each
calling several systems, held by toolpass to what the user may do there.

    open_pr      GitHub (open the pull request), Jira (comment, move to In Review), Slack (tell #shop-dev)
    ship         Kubernetes (read and patch the deployment), Slack (announce in #releases)
    report_bug   Linear (issue for Engineering), Jira (bug in SHOP), Slack (tell #shop-dev)
    status       Jira, GitHub and Kubernetes, read only

The tools are plain Python making plain HTTP requests, with the server's
own token. They carry no permission logic. toolpass sees each request, names
it (``github:pr.create on repo:acme/shop``), and asks the system itself,
through its integration, whether the user may do that; a request the user
may not make is refused before it is sent, and the tool stops there.

The user is ``DEMO_USER`` (dana@acme.dev by default), fixed by whoever starts
the server, never by the model. GitHub, Jira, Slack, Linear and Kubernetes
are stand-ins (``fake_*.py``) answering from the grants in ``world.py``,
started in this process, or shared with other servers through
``DEMO_SYSTEMS`` (``run.py`` does that); point the connections at real
systems and nothing else changes. With ``TOOLPASS_URL`` and
``TOOLPASS_API_KEY`` set, a toolpass server decides each request and
collects the runs for its dashboard; without them the engine runs here.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import fake_github
import fake_jira
import fake_kubernetes
import fake_linear
import fake_slack
import mcpstdio
import world

from toolpass import Session, Toolkit, Toolpass, as_agent


class Systems:
    """The five stand-ins, started, with the toolpass connections to them."""

    def __init__(self) -> None:
        self.fakes = {
            "github": fake_github.GitHubFake(),
            "jira": fake_jira.JiraFake(),
            "slack": fake_slack.SlackFake(),
            "linear": fake_linear.LinearFake(),
            "kubernetes": fake_kubernetes.KubernetesFake(),
        }
        modules = {"github": fake_github, "jira": fake_jira, "slack": fake_slack, "linear": fake_linear, "kubernetes": fake_kubernetes}
        self._secrets = tempfile.TemporaryDirectory(prefix="toolpass-demo-")
        self.urls = {name: fake.start() for name, fake in self.fakes.items()}
        self.connections = [modules[name].connection(url, Path(self._secrets.name)) for name, url in self.urls.items()]

    def shared(self) -> str:
        """For DEMO_SYSTEMS: other servers use these systems instead of starting their own."""
        return json.dumps({"urls": self.urls, "connections": self.connections})

    def close(self) -> None:
        for fake in self.fakes.values():
            fake.stop()
        self._secrets.cleanup()


class Endpoints:
    """Where the tools send their requests."""

    def __init__(self, urls: dict[str, str]) -> None:
        self.gh = f"{urls['github']}/api/v3"
        self.jira_api = f"{urls['jira']}/rest/api/3"
        self.slack_api = f"{urls['slack']}/api"
        self.linear_api = f"{urls['linear']}/graphql"
        self.k8s_api = urls["kubernetes"]


def http(method: str, url: str, body: Any = None, content_type: str = "application/json") -> Any:
    """One request with the server's own token, as any SDK would make it."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": content_type, "Authorization": "Bearer shop-server-token"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{method} {url} failed: {e.code} {e.read()[:200]!r}") from None
    return json.loads(raw) if raw else None


def build(connections: list[dict[str, Any]], urls: dict[str, str]) -> list[tuple[mcpstdio.Tool, dict[str, Any]]]:
    s = Endpoints(urls)
    if os.environ.get("TOOLPASS_URL"):
        # A toolpass server (run.py starts one) answers each permission question and collects the runs.
        tools = Toolkit(permissions=Toolpass.remote(), connections=connections, audit=None)
    else:
        tools = Toolkit(permissions=Toolpass(connections=connections), audit=None)

    def say(channel: str, text: str) -> None:
        http("POST", f"{s.slack_api}/chat.postMessage", {"channel": world.SLACK_CHANNELS[channel]["id"], "text": text})

    @tools.tool()
    def open_pr(repo: str, branch: str, title: str, issue: str) -> str:
        """Open a pull request from branch, link it on the Jira issue, move the issue to In Review and tell #shop-dev."""
        with as_agent():  # reading the repository is the agent's own business
            http("GET", f"{s.gh}/repos/{repo}")
        pr = http("POST", f"{s.gh}/repos/{repo}/pulls", {"title": title, "head": branch, "base": "main", "body": f"Fixes {issue}"})
        http("POST", f"{s.jira_api}/issue/{issue}/comment", {"body": f"Pull request: {pr['html_url']}"})
        moves = http("GET", f"{s.jira_api}/issue/{issue}/transitions")["transitions"]
        review = next(t["id"] for t in moves if t["name"] == "In Review")
        http("POST", f"{s.jira_api}/issue/{issue}/transitions", {"transition": {"id": review}})
        say("shop-dev", f"{title}: {pr['html_url']} ({issue} is in review)")
        return f"opened {repo}#{pr['number']} ({pr['html_url']}); {issue} moved to In Review; posted in #shop-dev"

    @tools.tool()
    def ship(service: str, version: str, env: str) -> str:
        """Deploy a version of a service to staging or prod, and announce it in #releases."""
        url = f"{s.k8s_api}/apis/apps/v1/namespaces/shop-{env}/deployments/{service}"
        current = http("GET", url)["spec"]["template"]["spec"]["containers"][0]
        image = current["image"].rsplit(":", 1)[0] + f":{version}"
        patch = {"spec": {"template": {"spec": {"containers": [{"name": current["name"], "image": image}]}}}}
        http("PATCH", url, patch, content_type="application/strategic-merge-patch+json")
        say("releases", f"{service} {version} is rolling out to {env}")
        return f"{service} on {env} now runs {image}; announced in #releases"

    @tools.tool()
    def report_bug(title: str, details: str) -> str:
        """File a bug for Engineering in Linear and in the SHOP Jira project, and tell #shop-dev."""
        mutation = "mutation Create($input: IssueCreateInput!) { issueCreate(input: $input) { success issue { identifier url } } }"
        team = fake_linear.TEAM_IDS["ENG"]
        made = http("POST", s.linear_api, {"query": mutation, "variables": {"input": {"teamId": team, "title": title, "description": details}}})
        linear = made["data"]["issueCreate"]["issue"]["identifier"]
        fields = {"project": {"key": world.JIRA_PROJECT}, "summary": title, "issuetype": {"name": "Bug"}, "description": f"{details}\n\nLinear: {linear}"}
        jira = http("POST", f"{s.jira_api}/issue", {"fields": fields})["key"]
        say("shop-dev", f"New bug: {title} ({linear}, {jira})")
        return f"filed {linear} in Linear and {jira} in Jira; posted in #shop-dev"

    @tools.tool()
    def status(issue: str) -> str:
        """Where things stand: the Jira issue, open pull requests on acme/shop, and what runs on staging and prod."""
        got = http("GET", f"{s.jira_api}/issue/{issue}")
        prs = http("GET", f"{s.gh}/repos/acme/shop/pulls")
        lines = [f"{issue}: {got['fields']['summary']} ({got['fields']['status']['name']})"]
        lines += [f"PR #{p['number']}: {p['title']}" for p in prs] or ["no open pull requests"]
        for env in ("staging", "prod"):
            d = http("GET", f"{s.k8s_api}/apis/apps/v1/namespaces/shop-{env}/deployments/shop-web")
            lines.append(f"shop-web on {env}: {d['spec']['template']['spec']['containers'][0]['image']}")
        return "\n".join(lines)

    read_only = {"readOnlyHint": True}
    writes = {"readOnlyHint": False, "destructiveHint": False}
    return [(open_pr, writes), (ship, writes), (report_bug, writes), (status, read_only)]


def main() -> None:
    os.environ.setdefault("TOOLPASS_SERVER_NAME", "shop")  # its name on the toolpass server's dashboard
    user = os.environ.get("DEMO_USER") or world.DANA
    shared = os.environ.get("DEMO_SYSTEMS")
    systems = None if shared else Systems()
    found = json.loads(shared) if shared else json.loads(systems.shared())  # type: ignore[union-attr]
    try:
        tools = build(found["connections"], found["urls"])
        session = Session(user)

        def call(fn: mcpstdio.Tool, args: dict[str, Any]) -> str:
            with session.active():
                return fn(**args)

        mcpstdio.serve("shop", tools, call)
    finally:
        if systems is not None:
            systems.close()


if __name__ == "__main__":
    main()

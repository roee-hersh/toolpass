"""A stand-in Jira Cloud site with project SHOP and its issues.

toolpass's jira integration finds a person's account with the user search
(``/rest/api/3/user/search``) and asks ``POST /rest/api/3/permissions/check``
whether that account holds a permission on the project or the issue, after
looking the project or issue up for its numeric id. This fake answers those
calls from ``JIRA_PERMISSIONS`` in ``world.py``: a person holds the same
permissions on the project and on every issue in it.

The demo's tools read issues, comment on them, move them through the
workflow and file new ones; the fake keeps those changes in memory.
"""

from __future__ import annotations

import base64
import itertools
import pathlib
from secrets import token_urlsafe
from typing import Any

from fakebase import Fake, Reply, Req
from world import ADMIN, JIRA_ISSUES, JIRA_PERMISSIONS, JIRA_PROJECT, PEOPLE

from toolpass.integrations.jira.actions import ACTIONS

API = "/rest/api/3"
PROJECT_ID = 10000
TOKEN = token_urlsafe(24)  # the API token of toolpass's account, ADMIN; written to a file by connection()
TRANSITIONS = {"11": "To Do", "21": "In Progress", "31": "In Review", "41": "Done"}  # id -> target status


def account_id(email: str) -> str:
    return "acc-" + email.split("@")[0]


def adf(text: str) -> dict[str, Any]:
    """Plain text as an Atlassian Document Format document."""
    return {"type": "doc", "version": 1, "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}]}


class JiraFake(Fake):
    name = "jira"

    def __init__(self) -> None:
        super().__init__()
        self.accounts = {account_id(e): e for e in PEOPLE}
        self.issues: dict[str, dict[str, Any]] = {}
        self.comment_ids = itertools.count(20001)
        for key, summary in JIRA_ISSUES.items():
            self._add(int(key.rsplit("-", 1)[1]), summary, "Bug", adf(summary))

    def _add(self, number: int, summary: str, issuetype: str, description: Any) -> dict[str, Any]:
        key = f"{JIRA_PROJECT}-{number}"
        issue = {
            "id": str(PROJECT_ID + 100 + number),
            "key": key,
            "fields": {
                "project": {"id": str(PROJECT_ID), "key": JIRA_PROJECT},
                "summary": summary,
                "issuetype": {"name": issuetype},
                "status": {"name": "To Do"},
                "description": description,
                "comment": {"comments": [], "total": 0},
            },
        }
        self.issues[key] = issue
        return issue

    def _is_admin(self, r: Req) -> bool:
        want = "Basic " + base64.b64encode(f"{ADMIN}:{TOKEN}".encode()).decode()
        return r.headers.get("authorization") == want

    def _holds(self, account: str, perm: str) -> bool:
        return perm in JIRA_PERMISSIONS.get(self.accounts.get(account, ""), set())

    def handle(self, r: Req) -> Reply:
        p, m = r.path, r.method
        with self.lock:
            # What toolpass asks, with its own account.
            if p == f"{API}/user/search":
                if not self._is_admin(r):
                    return Reply(200, [])  # Jira answers a credential it does not accept as an anonymous caller
                q = r.query.get("query", "").lower()
                return Reply(200, [self._user(e) for e in PEOPLE if q and q in e.lower()])
            if p == f"{API}/myself":
                return Reply(200, self._user(ADMIN)) if self._is_admin(r) else Reply(401, {"errorMessages": ["unauthorized"]})
            if p == f"{API}/permissions/check" and m == "POST":
                return self._check(r) if self._is_admin(r) else Reply(403, {"errorMessages": ["forbidden"]})
            if p == f"{API}/mypermissions":
                return Reply(200, {"permissions": {"ADMINISTER": {"havePermission": self._is_admin(r)}}})
            if p == f"{API}/permissions":
                return Reply(200, {"permissions": {k: {"key": k} for k in ACTIONS}})
            if p == f"{API}/project/{JIRA_PROJECT}":
                return Reply(200, {"id": str(PROJECT_ID), "key": JIRA_PROJECT, "name": "Shop"})
            # What the tools do.
            if p == f"{API}/issue" and m == "POST":
                return self._create(r.body)
            parts = p.removeprefix(f"{API}/issue/").split("/")
            if p.startswith(f"{API}/issue/") and parts[0] in self.issues:
                return self._issue(m, self.issues[parts[0]], parts[1:], r.body)
        return Reply(404, {"errorMessages": [f"no route {m} {p}"]})

    def _user(self, email: str) -> dict[str, Any]:
        return {"accountId": account_id(email), "accountType": "atlassian", "active": True, "emailAddress": email, "displayName": PEOPLE[email]}

    def _check(self, r: Req) -> Reply:
        """Every permission the request names, with the projects and issues
        (of project SHOP) the account holds it on."""
        account = r.body.get("accountId", "")
        ids = {int(i["id"]) for i in self.issues.values()}
        out = []
        for pp in r.body.get("projectPermissions") or []:
            for perm in pp.get("permissions") or []:
                held = self._holds(account, perm)
                out.append(
                    {
                        "permission": perm,
                        "projects": [x for x in pp.get("projects") or [] if held and x == PROJECT_ID],
                        "issues": [x for x in pp.get("issues") or [] if held and x in ids],
                    }
                )
        return Reply(200, {"projectPermissions": out, "globalPermissions": []})

    def _create(self, body: Any) -> Reply:
        f = (body.get("fields") or {}) if isinstance(body, dict) else {}
        if (f.get("project") or {}).get("key") != JIRA_PROJECT or not f.get("summary"):
            return Reply(400, {"errorMessages": ["fields.project.key SHOP and fields.summary are required"]})
        number = max(int(k.rsplit("-", 1)[1]) for k in self.issues) + 1
        desc = f.get("description")
        issue = self._add(number, f["summary"], (f.get("issuetype") or {}).get("name", "Task"), adf(desc) if isinstance(desc, str) else desc)
        return Reply(201, {"id": issue["id"], "key": issue["key"], "self": f"{self.url}{API}/issue/{issue['id']}"})

    def _issue(self, m: str, issue: dict[str, Any], rest: list[str], body: Any) -> Reply:
        f = issue["fields"]
        if m == "GET" and not rest:
            return Reply(200, issue)
        if rest == ["comment"] and m == "GET":
            return Reply(200, f["comment"])
        if rest == ["comment"] and m == "POST":
            text = body.get("body") if isinstance(body, dict) else None
            if not text:
                return Reply(400, {"errorMessages": ["body is required"]})
            comments = f["comment"]["comments"]
            c = {"id": str(next(self.comment_ids)), "body": adf(text) if isinstance(text, str) else text}
            comments.append(c)
            f["comment"]["total"] = len(comments)
            return Reply(201, c)
        if rest == ["transitions"] and m == "GET":
            return Reply(200, {"transitions": [{"id": i, "name": n, "to": {"name": n}} for i, n in TRANSITIONS.items()]})
        if rest == ["transitions"] and m == "POST":
            tid = (body.get("transition") or {}).get("id") if isinstance(body, dict) else None
            if tid not in TRANSITIONS:
                return Reply(400, {"errorMessages": [f"transition {tid!r} is not valid for this issue"]})
            f["status"] = {"name": TRANSITIONS[tid]}
            return Reply(204, b"")
        return Reply(404, {"errorMessages": [f"no route {m} on {issue['key']}"]})


def connection(url: str, secrets: pathlib.Path) -> dict[str, Any]:
    """The toolpass connection to the fake site, as toolpass's own account."""
    token = secrets / "jira-token"
    token.write_text(TOKEN)
    token.chmod(0o600)
    return {"id": "jira-acme", "integration": "jira", "url": url, "auth_mode": "basic", "username": ADMIN, "credential": f"file:{token}"}

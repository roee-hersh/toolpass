"""A stand-in for a GitHub Enterprise Server with one organization, acme.

toolpass reads GitHub as a GitHub App: it signs a JWT with the App's private
key, trades it for an installation token, maps an email to a login through
the organization's SAML identities (GraphQL) and reads the user's repository
permission from the collaborator permission endpoint. This fake answers
those calls from ``world.py``, plus the repository, pull request and comment
endpoints the demo's tools use. The REST API lives under ``/api/v3`` and
GraphQL at ``/api/graphql``, as on a real GitHub Enterprise Server.

acme/shop is private and forking is disabled, so a pull request needs push
access: dana (write) may open one, sam (read) may not.
"""

from __future__ import annotations

import base64
import json
import pathlib
import time
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fakebase import Fake, Reply, Req
from world import GITHUB_LOGINS, GITHUB_ORG, GITHUB_REPOS

CONNECTION_ID = "github-acme"
APP_ID = "Iv1.toolpassdemo"
INSTALLATION_ID = "42"
INSTALLATION_TOKEN = "ghs_demo_installation_token"

# The five booleans GitHub reports for each base role.
_LADDER = ("pull", "triage", "push", "maintain", "admin")
_RANK = {"none": 0, "read": 1, "triage": 2, "write": 3, "maintain": 4, "admin": 5}


def _jwt_issuer(authorization: str) -> str:
    """The iss claim of a bearer JWT, or "" when it is not one. The fake
    trusts the signature: it stands in for GitHub, not for its crypto."""
    parts = authorization.removeprefix("Bearer ").split(".")
    if len(parts) != 3:
        return ""
    try:
        claims = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
    except ValueError:
        return ""
    return str(claims.get("iss", "")) if isinstance(claims, dict) else ""


class GitHubFake(Fake):
    name = "github"

    def __init__(self) -> None:
        super().__init__()
        self.pulls: dict[str, list[dict[str, Any]]] = {repo: [] for repo in GITHUB_REPOS}
        self.comments: dict[tuple[str, int], list[dict[str, Any]]] = {}
        self.next_number = 1  # issues and pull requests share one sequence, as on GitHub
        self.next_comment = 1001

    def handle(self, r: Req) -> Reply:
        auth = r.headers.get("authorization", "")
        if r.path == "/api/graphql" and r.method == "POST":
            if auth != "Bearer " + INSTALLATION_TOKEN:
                return Reply(401, {"message": "Bad credentials"})
            return self.graphql(r.body if isinstance(r.body, dict) else {})
        if not r.path.startswith("/api/v3/"):
            return Reply(404, {"message": "Not Found"})
        seg = r.path.removeprefix("/api/v3/").split("/")
        # The App itself, with its JWT. Everything else is the tools' and
        # toolpass's installation-token calls; the tools' accept any credential.
        if seg[0] == "app" or seg[-1] == "installation":
            if _jwt_issuer(auth) != APP_ID:
                return Reply(401, {"message": "A JSON web token could not be decoded"})
            return self.app(r, seg)
        if seg[0] != "repos" or len(seg) < 3 or f"{seg[1]}/{seg[2]}" not in GITHUB_REPOS:
            return Reply(404, {"message": "Not Found"})
        return self.repo(r, f"{seg[1]}/{seg[2]}", seg[3:])

    def app(self, r: Req, seg: list[str]) -> Reply:
        if r.method == "GET" and seg == ["app"]:
            return Reply(200, {"slug": "toolpass-reader", "name": "toolpass reader"})
        if r.method == "GET" and seg == ["orgs", GITHUB_ORG, "installation"]:
            return Reply(200, {"id": int(INSTALLATION_ID), "account": {"login": GITHUB_ORG}, "permissions": {"metadata": "read", "members": "read"}})
        if r.method == "POST" and seg == ["app", "installations", INSTALLATION_ID, "access_tokens"]:
            expires = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 3600))
            return Reply(201, {"token": INSTALLATION_TOKEN, "expires_at": expires})
        return Reply(404, {"message": "Not Found"})

    def graphql(self, body: dict[str, Any]) -> Reply:
        """The organization's SAML identities: one per person, nameId the email."""
        variables = body.get("variables") or {}
        if variables.get("org") != GITHUB_ORG:
            return Reply(200, {"data": {"organization": None}})
        wanted = str(variables.get("email", "")).lower()  # the userName filter, when given
        nodes = [
            {"user": {"login": login}, "samlIdentity": {"nameId": email, "username": email}, "scimIdentity": None}
            for email, login in GITHUB_LOGINS.items()
            if not wanted or email.lower() == wanted
        ]
        page = {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": nodes}
        return Reply(200, {"data": {"organization": {"samlIdentityProvider": {"externalIdentities": page}}}})

    def repo(self, r: Req, full: str, rest: list[str]) -> Reply:
        owner, name = full.split("/")
        if r.method == "GET" and not rest:
            return Reply(200, self.repo_json(full))
        if r.method == "GET" and len(rest) == 3 and rest[0] == "collaborators" and rest[2] == "permission":
            if r.headers.get("authorization") != "Bearer " + INSTALLATION_TOKEN:
                return Reply(401, {"message": "Bad credentials"})
            return self.permission(full, rest[1])
        if rest == ["pulls"] and r.method == "GET":
            with self.lock:
                return Reply(200, list(self.pulls[full]))
        if rest == ["pulls"] and r.method == "POST":
            b = r.body if isinstance(r.body, dict) else {}
            if not b.get("title") or not b.get("head") or not b.get("base"):
                return Reply(422, {"message": "Validation Failed", "errors": [{"field": "title, head and base", "code": "missing_field"}]})
            with self.lock:
                n, self.next_number = self.next_number, self.next_number + 1
                pr = {
                    "number": n,
                    "state": "open",
                    "title": b["title"],
                    "body": b.get("body") or "",
                    "head": {"ref": b["head"]},
                    "base": {"ref": b["base"]},
                    "html_url": f"{self.url}/{owner}/{name}/pull/{n}",
                    "url": f"{self.url}/api/v3/repos/{full}/pulls/{n}",
                }
                self.pulls[full].append(pr)
            return Reply(201, pr)
        if len(rest) == 3 and rest[0] == "issues" and rest[2] == "comments" and rest[1].isdigit():
            n = int(rest[1])
            with self.lock:
                if not any(p["number"] == n for p in self.pulls[full]):
                    return Reply(404, {"message": "Not Found"})
                thread = self.comments.setdefault((full, n), [])
                if r.method == "GET":
                    return Reply(200, list(thread))
                if r.method == "POST":
                    text = r.body.get("body", "") if isinstance(r.body, dict) else ""
                    if not text:
                        return Reply(422, {"message": "Validation Failed"})
                    cid, self.next_comment = self.next_comment, self.next_comment + 1
                    c = {"id": cid, "body": text, "html_url": f"{self.url}/{owner}/{name}/pull/{n}#issuecomment-{cid}"}
                    thread.append(c)
                    return Reply(201, c)
        return Reply(404, {"message": "Not Found"})

    def repo_json(self, full: str) -> dict[str, Any]:
        owner, name = full.split("/")
        return {
            "name": name,
            "full_name": full,
            "owner": {"login": owner, "type": "Organization"},
            "private": True,
            "visibility": "private",
            "default_branch": "main",
            "has_issues": True,
            "allow_forking": False,
            "html_url": f"{self.url}/{full}",
        }

    def permission(self, full: str, login: str) -> Reply:
        """GitHub's effective permission of a login on a repository."""
        email = next((e for e, lg in GITHUB_LOGINS.items() if lg.lower() == login.lower()), None)
        if email is None:
            return Reply(404, {"message": "Not Found"})
        role = GITHUB_REPOS[full].get(email, "none")
        perms = {p: i < _RANK[role] for i, p in enumerate(_LADDER)}
        return Reply(200, {"permission": role, "role_name": role, "user": {"login": GITHUB_LOGINS[email], "permissions": perms}})


def connection(url: str, secrets: pathlib.Path) -> dict[str, Any]:
    """The toolpass connection for the fake: a GitHub App with a freshly
    generated private key, identities from SAML."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = secrets / "github-app.pem"
    pem.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    pem.chmod(0o600)
    return {
        "id": CONNECTION_ID,
        "integration": "github",
        "url": url,
        "organization": GITHUB_ORG,
        "app_id": APP_ID,
        "installation_id": INSTALLATION_ID,
        "credential": f"file:{pem}",
        "identity_mode": "saml",
    }

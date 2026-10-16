"""A stand-in for Linear's GraphQL API at ``/graphql``.

It answers the queries toolpass's linear integration sends (a user by email,
the user's team memberships, a team by key or id, an issue, the API key's own
viewer) from ``world.py``, and the two operations the demo's tools send: the
``issueCreate`` mutation and an ``issues`` query filtered by team. Issues the
tools create are kept and listed afterwards.

Linear names users, teams and issues by uuid, and toolpass reads a ``team:``
resource as a team key (ENG) or a uuid, so each team in ``world.py`` gets a
fixed uuid here: ``TEAM_IDS["ENG"]`` is the ``teamId`` a tool sends.

Only the top-level fields of an operation are read (with toolpass's own
GraphQL reader); every field the caller could select is returned.
"""

from __future__ import annotations

import pathlib
import uuid
from collections.abc import Callable
from typing import Any

from fakebase import Fake, Reply, Req
from world import ADMIN, LINEAR_TEAMS, PEOPLE

from toolpass.integrations.linear.routes import read_operation

_NS = uuid.UUID("5b4b1f9e-3c56-4d7e-9a0b-6c1d2e3f4a5b")


def _id(name: str) -> str:
    """A stable Linear-style uuid for a world.py name."""
    return str(uuid.uuid5(_NS, name))


TEAM_IDS = {key: _id(str(t["id"])) for key, t in LINEAR_TEAMS.items()}
USER_IDS = {email: _id(email) for email in PEOPLE}


def _value(v: Any, variables: dict[str, Any]) -> Any:
    """A parsed GraphQL argument with its variables filled in."""
    if isinstance(v, tuple):
        kind, text = v
        if kind == "$":
            return variables.get(text)
        return {"true": True, "false": False, "null": None}.get(text, text)
    if isinstance(v, dict):
        return {k: _value(x, variables) for k, x in v.items()}
    if isinstance(v, list):
        return [_value(x, variables) for x in v]
    return v


def _eq(flt: Any, *path: str) -> Any:
    """``flt[path...]["eq"]`` (or ``eqIgnoreCase``), else None."""
    for p in path:
        flt = flt.get(p) if isinstance(flt, dict) else None
    if not isinstance(flt, dict):
        return None
    return flt.get("eq", flt.get("eqIgnoreCase"))


class LinearFake(Fake):
    name = "linear"

    def __init__(self) -> None:
        super().__init__()
        self.issues: list[dict[str, Any]] = []  # created by the tools, oldest first
        self.answers: dict[tuple[str, str], Callable[[dict[str, Any]], Any]] = {
            ("query", "users"): self._query_users,
            ("query", "user"): self._query_user,
            ("query", "teams"): self._query_teams,
            ("query", "issue"): self._query_issue,
            ("query", "viewer"): self._query_viewer,
            ("query", "organization"): self._query_organization,
            ("query", "issues"): self._query_issues,
            ("mutation", "issueCreate"): self._mutation_issue_create,
        }

    # -- shapes --

    def _user(self, email: str) -> dict[str, Any]:
        return {
            "id": USER_IDS[email],
            "email": email,
            "name": PEOPLE[email],
            "active": True,
            "admin": email == ADMIN,
            "owner": False,
            "guest": False,
            "app": False,
            "disableReason": None,
        }

    def _team(self, key: str) -> dict[str, Any]:
        t = LINEAR_TEAMS[key]
        return {"id": TEAM_IDS[key], "key": key, "name": str(t["name"]), "visibility": "public", "archivedAt": None}

    def _key(self, team_id: Any) -> str | None:
        """The key of the team with this uuid or key."""
        return next((k for k, i in TEAM_IDS.items() if team_id in (i, k)), None)

    # -- the endpoint --

    def handle(self, r: Req) -> Reply:
        if r.method != "POST" or r.path != "/graphql":
            return Reply(404, {"message": "not found"})
        if not r.headers.get("authorization"):
            return _error("authentication error", "Authentication required", 401)
        body = r.body if isinstance(r.body, dict) else {}
        variables = body.get("variables") or {}
        try:
            kind, fields = read_operation(str(body.get("query") or ""))
        except ValueError as e:
            return _error("graphql error", f"Syntax Error: {e}", 400)
        data: dict[str, Any] = {}
        with self.lock:
            for name, raw in fields:
                args = {k: _value(v, variables) for k, v in raw.items()}
                answer = self.answers.get((kind, name))
                if answer is None:
                    return _error("graphql error", f'Cannot query field "{name}" on type "{kind.title()}"', 400)
                out = answer(args)
                if isinstance(out, Reply):
                    return out
                data[name] = out
        return Reply(200, {"data": data})

    # -- what toolpass asks --

    def _query_users(self, args: dict[str, Any]) -> Any:
        email = str(_eq(args.get("filter"), "email") or "").lower()
        return {"nodes": [self._user(e) for e in PEOPLE if e.lower() == email]}

    def _query_user(self, args: dict[str, Any]) -> Any:
        email = next((e for e, i in USER_IDS.items() if i == args.get("id")), None)
        if email is None:
            return _not_found("User")
        teams = [{"owner": False, "team": {"id": TEAM_IDS[k], "key": k}} for k, t in LINEAR_TEAMS.items() if email in t["members"]]
        return {"teamMemberships": {"nodes": teams, "pageInfo": {"hasNextPage": False, "endCursor": None}}}

    def _query_teams(self, args: dict[str, Any]) -> Any:
        flt = args.get("filter")
        want = _eq(flt, "key") or _eq(flt, "id")
        return {"nodes": [self._team(k) for k in LINEAR_TEAMS if want in (k, TEAM_IDS[k])]}

    def _query_issue(self, args: dict[str, Any]) -> Any:
        issue = next((i for i in self.issues if args.get("id") in (i["id"], i["identifier"])), None)
        if issue is None:
            return _not_found("Issue")
        return {**issue, "trashed": False, "archivedAt": None, "team": self._team(issue["team"]["key"])}

    def _query_viewer(self, args: dict[str, Any]) -> Any:
        return self._user(ADMIN)

    def _query_organization(self, args: dict[str, Any]) -> Any:
        return {"id": _id("acme"), "name": "Acme", "urlKey": "acme"}

    # -- what the tools do --

    def _query_issues(self, args: dict[str, Any]) -> Any:
        key = self._key(_eq(args.get("filter"), "team", "id")) or self._key(_eq(args.get("filter"), "team", "key"))
        return {"nodes": [i for i in self.issues if not args.get("filter") or i["team"]["key"] == key]}

    def _mutation_issue_create(self, args: dict[str, Any]) -> Any:
        inp: dict[str, Any] = args["input"] if isinstance(args.get("input"), dict) else {}
        key = self._key(inp.get("teamId"))
        if key is None or not inp.get("title"):
            return _error("invalid input", "Argument Validation Error: teamId and title are required", 400)
        n = sum(1 for i in self.issues if i["team"]["key"] == key) + 1
        ident = f"{key}-{n}"
        issue = {
            "id": str(uuid.uuid4()),
            "identifier": ident,
            "url": f"https://linear.app/acme/issue/{ident.lower()}",
            "title": inp["title"],
            "description": inp.get("description") or "",
            "team": {"id": TEAM_IDS[key], "key": key},
        }
        self.issues.append(issue)
        return {"success": True, "issue": issue}


def _error(typ: str, message: str, status: int) -> Reply:
    return Reply(status, {"errors": [{"message": message, "extensions": {"type": typ, "userError": True}}]})


def _not_found(what: str) -> Reply:
    return _error("invalid input", f"Entity not found: {what}", 200)


def connection(url: str, secrets: pathlib.Path) -> dict[str, Any]:
    """The toolpass connection ``linear-acme`` for the fake started at ``url``:
    its GraphQL endpoint, and a generated API key written under ``secrets``."""
    key = secrets / "linear-api-key"
    key.write_text(f"lin_api_{uuid.uuid4().hex}\n")
    key.chmod(0o600)
    endpoint = url if url.endswith("/graphql") else url.rstrip("/") + "/graphql"
    return {"id": "linear-acme", "integration": "linear", "url": endpoint, "credential": f"file:{key}"}

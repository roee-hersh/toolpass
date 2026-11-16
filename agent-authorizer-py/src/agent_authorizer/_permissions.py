"""Authorization answered by agent-authorizer: may this user do this, asked live of
the system that owns the resource.

    import agent_authorizer
    from agent_authorizer import AgentAuthorizer, permission_check
    tp = AgentAuthorizer.from_config("agent-authorizer.yaml")          # or AgentAuthorizer.remote(url, key)

    @agent_authorizer.tool(effect="write", authorize=permission_check(tp, "github-acme", "pr.create", "repo:{repo}"))
    def open_pr(repo: str, title: str) -> str: ...

Works with anything that has agent-authorizer's ``check`` method, in-process or
remote; agent-authorizer does not import agent_authorizer itself.
"""

from __future__ import annotations

import string
from typing import Any, Protocol

from agent_authorizer._toolkit import AuthDecision, Call, refusal_code


class _Checker(Protocol):
    def check(self, user: str, connection: str, action: str, resource: str, groups: Any = None, *, fresh: bool = False, token: str | None = None) -> Any: ...


def permission_check(tp: _Checker, connection: str, action: str, resource: str, *, fresh: bool = False) -> Any:
    """An authorizer that asks agent-authorizer about the session's user.

    ``resource`` is a format string over the call's arguments, e.g.
    ``"issue:{key}"``. Only agent-authorizer's ``allow`` lets the call through:
    ``deny``, ``unknown`` and an unreachable agent-authorizer all refuse, with
    distinct codes: ``not_authorized`` for a deny, ``not_configured`` for a
    connection agent-authorizer does not have, ``authorization_error`` for anything
    that could not be checked. Use
    ``fresh=True`` on destructive tools to skip agent-authorizer's caches.
    """

    fields = set()
    for _, field, _, _ in string.Formatter().parse(resource):
        if field is None:
            continue
        name = field.split(".")[0].split("[")[0]
        if not name or name.isdigit():
            raise ValueError(f"permission_check: resource {resource!r} must name the tool's parameters, as in 'issue:{{key}}'")
        fields.add(name)

    def authorize(call: Call) -> AuthDecision:
        target = resource.format(**call.arguments)
        groups = None if call.session.groups is None else list(call.session.groups)
        # The token only when the session has one, so a checker written
        # before the argument existed keeps working.
        extra = {"token": call.session.token} if call.session.token else {}
        d = tp.check(call.session.user, connection, action, target, groups, fresh=fresh, **extra)
        outcome = getattr(d, "decision", None)
        reason = f"agent-authorizer {outcome} {action} on {target} in {connection}: {getattr(d, 'reason', '')}"
        if outcome == "allow":
            return AuthDecision(True, reason)
        return AuthDecision(False, reason, refusal_code(d))

    authorize.__name__ = f"permission_check({connection}, {action})"
    authorize.__agent_authorizer_fields__ = frozenset(fields)  # type: ignore[attr-defined]
    return authorize

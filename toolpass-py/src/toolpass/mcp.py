"""The user of an MCP server's request, from the access token it was sent with.

For a server people connect to directly (Claude Desktop, Claude Code, an IDE)
and sign in to with their own account, the official ``mcp`` SDK verifies each
request's token (``MCPServer(token_verifier=..., auth=...)``). ``token_session``
makes the user in that token the user every ``toolpass.tool`` checks:

    import toolpass
    from toolpass.mcp import token_session

    toolpass.configure(permissions=tp, session=token_session(), on_refuse=str)

The user is the token's ``email`` claim (``user_claim="sub"`` for its subject),
and a token whose ``email_verified`` is false names no one. The tool calls
made with one token share one ``Session``, so limits and the session checks
hold across them. A request without a verified token has no user, and a
secured tool refuses it (``no_session``).

A server only your agent reaches, where the agent names the user it
authenticated itself, uses ``session_from_header`` instead.
"""

from __future__ import annotations

import collections
import hashlib
import threading
from collections.abc import Callable
from typing import Any

from toolpass._session import Session

__all__ = ["token_session"]

_REMEMBERED = 4096  # tokens a source keeps a Session for; the least recently used goes first
_MISSING = object()


def _claim(token: Any, claim: str) -> Any:
    claims = getattr(token, "claims", None) or {}
    if claim == "sub":
        subject = getattr(token, "subject", None)
        return subject if subject is not None else claims.get("sub", _MISSING)
    return claims.get(claim, _MISSING)


def _user(token: Any, user_claim: str) -> str | None:
    user = _claim(token, user_claim)
    if not isinstance(user, str) or not user.strip():
        return None
    if user_claim == "email":
        verified = _claim(token, "email_verified")
        if verified is False or (isinstance(verified, str) and verified.strip().lower() == "false"):
            return None  # whoever set the address has not shown they own it
    return user.strip()


def token_session(user_claim: str = "email", groups_claim: str | None = None) -> Callable[[], Session | None]:
    """A session source (``Toolkit(session=...)``, ``toolpass.configure(session=...)``)
    that gives each request the user of its verified access token.

    ``groups_claim`` names a claim holding the user's groups, as a list of
    strings, for the systems that grant by group. The token itself travels
    with every check, for a connection whose ``groups_connection`` is an
    oidc connection that reads the groups from it."""
    try:
        from mcp.server.auth.middleware.auth_context import get_access_token
    except ImportError as e:  # pragma: no cover - exercised only without the SDK
        raise ImportError("toolpass.mcp.token_session needs the mcp SDK: pip install mcp") from e

    remembered: collections.OrderedDict[str, Session] = collections.OrderedDict()
    lock = threading.Lock()

    def source() -> Session | None:
        token = get_access_token()
        if token is None:
            return None
        user = _user(token, user_claim)
        if user is None:
            return None
        groups: tuple[str, ...] | None = None
        if groups_claim is not None:
            raw = _claim(token, groups_claim)
            if not isinstance(raw, list) or not all(isinstance(g, str) for g in raw):
                return None  # groups were asked for and the token does not carry them
            groups = tuple(raw)
        k = hashlib.sha256(str(getattr(token, "token", "")).encode()).hexdigest() + "\0" + user
        with lock:
            session = remembered.get(k)
            if session is None:
                session = remembered[k] = Session(user, groups=groups, token=str(getattr(token, "token", "") or "") or None)
                while len(remembered) > _REMEMBERED:
                    remembered.popitem(last=False)
            else:
                remembered.move_to_end(k)
            return session

    return source

"""``session_from_header``: the user of each HTTP request from X-Toolpass-User, as a plain ASGI wrapper."""

from __future__ import annotations

import asyncio
from typing import Any

from toolpass import GROUPS_HEADER, KEY_HEADER, SESSION_HEADER, TOKEN_HEADER, USER_HEADER, Session, current_session, session_from_header

SEEN: list[Session | None] = []
SENT: list[dict[str, Any]] = []
REFUSED_KEY = (403, "toolpass: this server needs the X-Toolpass-Key header, and this request's is missing or wrong")


async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
    SEEN.append(current_session())
    if scope["type"] == "http":
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})
    else:
        await send({"type": "websocket.accept"})


async def send(message: dict[str, Any]) -> None:
    SENT.append(message)


async def receive() -> dict[str, Any]:
    return {"type": "http.request"}


def call(wrapped: Any, headers: list[tuple[bytes, bytes]], method: str = "POST") -> tuple[int, str]:
    SEEN.clear()
    SENT.clear()
    asyncio.run(wrapped({"type": "http", "method": method, "headers": headers}, receive, send))
    start = next(m for m in SENT if m["type"] == "http.response.start")
    return start["status"], b"".join(m.get("body", b"") for m in SENT if m["type"] == "http.response.body").decode()


def users() -> list[str | None]:
    return [s.user if s else None for s in SEEN]


def test_header_names_the_user() -> None:
    wrapped = session_from_header(app)
    assert call(wrapped, [(b"x-toolpass-user", b" dana@example.com ")]) == (200, "ok")
    assert users() == ["dana@example.com"]
    assert SEEN[0] is not None and SEEN[0].groups is None
    assert current_session() is None, "the session ends with the request"


def test_groups() -> None:
    wrapped = session_from_header(app)
    assert call(wrapped, [(b"x-toolpass-user", b"dana@example.com"), (b"x-toolpass-groups", b" platform, , sre ")]) == (200, "ok")
    assert SEEN[0] is not None and SEEN[0].groups == ("platform", "sre")
    assert call(wrapped, [(b"x-toolpass-user", b"dana@example.com"), (b"x-toolpass-groups", b"a"), (b"x-toolpass-groups", b"b")]) == (
        400,
        "toolpass: the request carries more than one X-Toolpass-Groups header",
    )
    assert SEEN == []


def test_token() -> None:
    wrapped = session_from_header(app)
    dana, sid = (b"x-toolpass-user", b"dana@example.com"), (b"mcp-session-id", b"s1")
    assert call(wrapped, [dana, sid, (b"x-toolpass-token", b" eyJ.a.b ")]) == (200, "ok")
    first = SEEN[0]
    assert first is not None and first.token == "eyJ.a.b"
    # A refreshed token keeps the conversation's session and carries the new token.
    call(wrapped, [dana, sid, (b"x-toolpass-token", b"eyJ.c.d")])
    assert SEEN[0] is first and first.token == "eyJ.c.d"
    call(wrapped, [dana, sid])
    assert SEEN[0] is first and first.token == "eyJ.c.d", "a request without the header keeps the last token"
    assert call(wrapped, [dana, (b"x-toolpass-token", b"a"), (b"x-toolpass-token", b"b")]) == (
        400,
        "toolpass: the request carries more than one X-Toolpass-Token header",
    )
    assert "token=" not in repr(first)


def test_no_header_means_no_session() -> None:
    """Even a session the process made current does not serve a request that names nobody."""
    wrapped = session_from_header(app)
    with Session("admin@example.com").active():
        assert call(wrapped, []) == (200, "ok")
        assert users() == [None]
        assert current_session() is not None and current_session().user == "admin@example.com", "and it is back after the request"  # type: ignore[union-attr]


def test_bad_header_is_refused() -> None:
    wrapped = session_from_header(app)
    assert call(wrapped, [(b"x-toolpass-user", b"")]) == (400, "toolpass: the X-Toolpass-User header is empty")
    assert call(wrapped, [(b"x-toolpass-user", b"\xff")]) == (400, "toolpass: the X-Toolpass-User header is empty")
    assert call(wrapped, [(b"x-toolpass-user", b"a@x"), (b"x-toolpass-user", b"b@x")]) == (
        400,
        "toolpass: the request carries more than one X-Toolpass-User header",
    )
    assert SEEN == [], "the app never ran"


def test_key() -> None:
    wrapped = session_from_header(app, key="s3cret")
    assert call(wrapped, [(b"x-toolpass-user", b"dana@example.com")]) == REFUSED_KEY
    assert call(wrapped, [(b"x-toolpass-user", b"dana@example.com"), (b"x-toolpass-key", b"s3cre")]) == REFUSED_KEY
    assert call(wrapped, [(b"x-toolpass-key", b"s3cret"), (b"x-toolpass-key", b"s3cret")]) == REFUSED_KEY
    assert SEEN == []
    assert call(wrapped, [(b"x-toolpass-user", b"dana@example.com"), (b"x-toolpass-key", b"s3cret")]) == (200, "ok")
    assert users() == ["dana@example.com"]


def test_one_session_per_mcp_session() -> None:
    """Requests carrying the same Mcp-Session-Id share one Session, so what the
    session remembers (counts, untrusted text, private reads) carries across the
    tool calls of a conversation; another id, user or group set gets its own,
    and a DELETE (the end of the MCP session) drops it."""
    wrapped = session_from_header(app)
    dana, sid = (b"x-toolpass-user", b"dana@example.com"), (b"mcp-session-id", b"s1")
    call(wrapped, [dana, sid])
    first = SEEN[0]
    call(wrapped, [dana, sid])
    assert SEEN[0] is first
    call(wrapped, [dana, (b"mcp-session-id", b"s2")])
    assert SEEN[0] is not first
    call(wrapped, [(b"x-toolpass-user", b"admin@example.com"), sid])
    assert SEEN[0] is not first and SEEN[0] is not None and SEEN[0].user == "admin@example.com"
    call(wrapped, [dana, sid, (b"x-toolpass-groups", b"g")])
    assert SEEN[0] is not first
    call(wrapped, [dana])
    assert SEEN[0] is not first, "no id: a session of its own"
    call(wrapped, [dana, sid], method="DELETE")
    assert SEEN[0] is first
    call(wrapped, [dana, sid])
    assert SEEN[0] is not first, "after the DELETE, a new one"
    call(wrapped, [dana, sid, (b"mcp-session-id", b"s3")])
    assert SEEN[0] is not first, "two ids: none"


def test_one_session_per_named_conversation() -> None:
    """The agent names the conversation itself in X-Toolpass-Session, which
    wins over the MCP session id and survives the MCP session's DELETE."""
    wrapped = session_from_header(app)
    dana, conv = (b"x-toolpass-user", b"dana@example.com"), (b"x-toolpass-session", b"thread-7")
    call(wrapped, [dana, conv, (b"mcp-session-id", b"s1")])
    first = SEEN[0]
    call(wrapped, [dana, conv, (b"mcp-session-id", b"s2")])
    assert SEEN[0] is first
    call(wrapped, [dana, conv], method="DELETE")
    call(wrapped, [dana, conv])
    assert SEEN[0] is first
    call(wrapped, [dana, (b"x-toolpass-session", b"thread-8")])
    assert SEEN[0] is not first
    call(wrapped, [dana, (b"x-toolpass-session", b" ")])
    assert SEEN[0] is not first, "blank: a session of its own"
    assert call(wrapped, [dana, conv, conv]) == (400, "toolpass: the request carries more than one X-Toolpass-Session header")


def test_websocket_is_held_to_the_same_headers() -> None:
    def ws(wrapped: Any, headers: list[tuple[bytes, bytes]]) -> dict[str, Any]:
        SEEN.clear()
        SENT.clear()
        asyncio.run(wrapped({"type": "websocket", "headers": headers}, receive, send))
        return SENT[0]

    wrapped = session_from_header(app, key="s3cret")
    assert ws(wrapped, [(b"x-toolpass-user", b"dana@example.com")]) == {"type": "websocket.close", "code": 4403, "reason": REFUSED_KEY[1]}
    assert ws(wrapped, [(b"x-toolpass-key", b"s3cret"), (b"x-toolpass-user", b"")]) == {
        "type": "websocket.close",
        "code": 4400,
        "reason": "toolpass: the X-Toolpass-User header is empty",
    }
    assert SEEN == []
    assert ws(wrapped, [(b"x-toolpass-key", b"s3cret"), (b"x-toolpass-user", b"dana@example.com")]) == {"type": "websocket.accept"}
    assert users() == ["dana@example.com"]


def test_other_scopes_pass_through() -> None:
    async def lifespan_app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        SEEN.append(scope["type"])

    SEEN.clear()
    asyncio.run(session_from_header(lifespan_app, key="k")({"type": "lifespan"}, receive, send))
    assert SEEN == ["lifespan"]


def test_header_names() -> None:
    assert (USER_HEADER, GROUPS_HEADER, SESSION_HEADER, KEY_HEADER, TOKEN_HEADER) == (
        "X-Toolpass-User",
        "X-Toolpass-Groups",
        "X-Toolpass-Session",
        "X-Toolpass-Key",
        "X-Toolpass-Token",
    )


def call_from(wrapped: Any, client: Any, headers: list[tuple[bytes, bytes]]) -> tuple[int, str]:
    SEEN.clear()
    SENT.clear()
    asyncio.run(wrapped({"type": "http", "method": "POST", "headers": headers, "client": client}, receive, send))
    start = next(m for m in SENT if m["type"] == "http.response.start")
    return start["status"], b"".join(m.get("body", b"") for m in SENT if m["type"] == "http.response.body").decode()


def test_without_a_key_only_this_machine_gets_in() -> None:
    """Anyone who reaches the server can set the header, so without key= it serves this machine only."""
    wrapped = session_from_header(app)
    user = [(b"x-toolpass-user", b"dana@example.com")]
    for local in (("127.0.0.1", 5000), ("::1", 5000), ("::ffff:127.0.0.1", 5000), None):
        assert call_from(wrapped, local, user) == (200, "ok"), local
    for remote in (("10.0.0.7", 5000), ("192.168.1.2", 5000), ("1270.0.0.1", 1)):
        assert call_from(wrapped, remote, user) == (
            403,
            "toolpass: this server takes requests from this machine only; give it key= to serve agents elsewhere",
        ), remote
        assert SEEN == [], "refused before the app runs"
    keyed = session_from_header(app, key="k")
    assert call_from(keyed, ("10.0.0.7", 5000), [*user, (b"x-toolpass-key", b"k")]) == (200, "ok")
    assert users() == ["dana@example.com"]


def test_the_header_never_speaks_for_an_authenticated_caller() -> None:
    wrapped = session_from_header(app)
    assert call(wrapped, [(b"authorization", b"Bearer t"), (b"x-toolpass-user", b"admin@example.com")]) == (
        400,
        "toolpass: a request that authenticates with its own token cannot also name its user in X-Toolpass-User",
    )
    assert SEEN == []
    assert call(wrapped, [(b"authorization", b"Bearer t")]) == (200, "ok"), "a token alone goes to the server's own auth"
    assert users() == [None]

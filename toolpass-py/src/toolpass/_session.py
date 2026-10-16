"""Sessions: who is asking, and what has happened so far in one conversation.

The application creates a ``Session`` for the user it authenticated and makes
it current for the duration of a request or conversation turn. Tools read the
user from it, never from the model's arguments. The session also remembers
what its tools did: whether untrusted content or private data reached the
model, how many calls each tool and each effect made.
"""

from __future__ import annotations

import collections
import contextlib
import contextvars
import hmac
import threading
import uuid
from collections.abc import Awaitable, Callable, Iterable, Iterator, MutableMapping, Sequence
from typing import Any

from toolpass._untrusted import DEFAULT_CAPACITY, DEFAULT_NGRAM, UntrustedText

_current: contextvars.ContextVar[Session | None] = contextvars.ContextVar("toolpass_session")


class Session:
    """One user's conversation with an agent.

    ``user`` is the identity the application authenticated; ``groups`` are
    optional group names passed to authorization. ``ngram`` is how many
    consecutive words of untrusted output an argument must repeat to count as
    carrying it.

        session = Session("dana@example.com")
        with session.active():
            agent.run(prompt)
    """

    def __init__(
        self,
        user: str,
        *,
        groups: Iterable[str] | None = None,
        id: str | None = None,
        ngram: int = DEFAULT_NGRAM,
        untrusted_capacity: int = DEFAULT_CAPACITY,
    ) -> None:
        if not isinstance(user, str) or not user.strip():
            raise ValueError("user must be a non-empty string")
        self.user = user
        self.groups: tuple[str, ...] | None = None if groups is None else tuple(groups)
        self.id = id or uuid.uuid4().hex
        self._lock = threading.Lock()
        self._untrusted = UntrustedText(ngram, untrusted_capacity)
        self._saw_untrusted = False
        self._read_private = False
        self._counts: dict[str, int] = {}

    def __repr__(self) -> str:
        return f"Session(user={self.user!r}, id={self.id!r})"

    @contextlib.contextmanager
    def active(self) -> Iterator[Session]:
        """Make this the current session inside the ``with`` block."""
        token = _current.set(self)
        try:
            yield self
        finally:
            _current.reset(token)

    @property
    def saw_untrusted(self) -> bool:
        """True once a tool marked ``untrusted_output`` ran in this session."""
        with self._lock:
            return self._saw_untrusted

    @property
    def read_private(self) -> bool:
        """True once a tool marked ``reads_private`` ran in this session."""
        with self._lock:
            return self._read_private

    def count(self, key: str) -> int:
        """Calls counted so far under ``key`` ("tool:<name>" or "effect:<effect>")."""
        with self._lock:
            return self._counts.get(key, 0)

    # The toolkit's side. Kept out of the public names on purpose.

    def _reserve(self, caps: Sequence[tuple[str, int]]) -> str | None:
        """Take one call from every cap, all or none. Returns the first key that
        is exhausted, or None when every cap had room and was taken."""
        with self._lock:
            for key, cap in caps:
                if self._counts.get(key, 0) >= cap:
                    return key
            for key, _ in caps:
                self._counts[key] = self._counts.get(key, 0) + 1
            return None

    def _release(self, caps: Sequence[tuple[str, int]]) -> None:
        with self._lock:
            for key, _ in caps:
                self._counts[key] = max(0, self._counts.get(key, 0) - 1)

    def _mark(self, *, untrusted: bool = False, private: bool = False) -> None:
        with self._lock:
            self._saw_untrusted = self._saw_untrusted or untrusted
            self._read_private = self._read_private or private

    def _record_untrusted(self, value: object) -> None:
        with self._lock:
            self._saw_untrusted = True
            self._untrusted.add(value)

    def _carries_untrusted(self, values: Iterable[object]) -> bool:
        with self._lock:
            return self._untrusted.matches(values)


def current_session() -> Session | None:
    """The session made current with ``Session.active()``, or None."""
    return _current.get(None)


USER_HEADER = "X-Toolpass-User"
GROUPS_HEADER = "X-Toolpass-Groups"
SESSION_HEADER = "X-Toolpass-Session"
KEY_HEADER = "X-Toolpass-Key"
_REMEMBERED = 4096  # MCP sessions a wrapper keeps a Session for; the least recently used goes first

_Scope = MutableMapping[str, Any]
_Receive = Callable[[], Awaitable[MutableMapping[str, Any]]]
_Send = Callable[[MutableMapping[str, Any]], Awaitable[None]]
_ASGIApp = Callable[[_Scope, _Receive, _Send], Awaitable[None]]
_SessionKey = tuple[str, tuple[str, ...] | None, bytes]


def session_from_header(app: _ASGIApp, *, key: str | None = None) -> _ASGIApp:
    """Run each request of an ASGI app under a ``Session`` for the user the
    agent names in the ``X-Toolpass-User`` header.

    For an MCP server over Streamable HTTP, wrap the server's app::

        app = session_from_header(mcp.streamable_http_app())   # then uvicorn.run(app, ...)

    The agent that calls the server sets the header to the user it
    authenticated (an SSO session, the Slack user who wrote the message),
    once, on its MCP client; ``X-Toolpass-Groups``, comma-separated, names
    the user's groups for the systems that grant by group. Every
    ``secured_tool`` the server runs then checks that user. The requests of
    one conversation share one ``Session``, so its limits, its
    untrusted-input check and its exfiltration guard hold across the tool
    calls of the conversation: the agent names the conversation in
    ``X-Toolpass-Session`` (its own chat or thread id), or else the MCP
    session (the ``Mcp-Session-Id`` header, which a sessionless client does
    not have) is the conversation. The ``Session`` ends with the MCP session
    (its DELETE) or when the wrapper has 4096 newer ones; a request naming
    neither gets a ``Session`` of its own.

    A request without the user header runs under no session at all, even
    when the process made one current, so a secured tool refuses it
    (``no_session``). A request with an empty or doubled header is refused
    with 400.

    Whoever can reach the server can set the header. On a private network
    (localhost, the agent's own pod, a network only the agent can reach) that
    is the agent. A server reachable by others takes ``key``: a secret the
    agent sends in ``X-Toolpass-Key`` on every request, and a request without
    it is refused with 403 before anything runs. A websocket connection is
    held to the same headers (refused with a close code 4403 or 4400). A
    server whose callers authenticate themselves (the SDK's token auth) takes
    the user from that, not from this header: here the header would win.
    """

    remembered: collections.OrderedDict[_SessionKey, Session] = collections.OrderedDict()
    lock = threading.Lock()

    def session_for(user: str, groups: tuple[str, ...] | None, sid: bytes | None) -> Session:
        if sid is None:
            return Session(user, groups=groups)
        k = (user, groups, sid)
        with lock:
            session = remembered.get(k)
            if session is None:
                session = remembered[k] = Session(user, groups=groups)
                while len(remembered) > _REMEMBERED:
                    remembered.popitem(last=False)
            else:
                remembered.move_to_end(k)
            return session

    async def wrapped(scope: _Scope, receive: _Receive, send: _Send) -> None:
        kind = scope.get("type")
        if kind not in ("http", "websocket"):
            await app(scope, receive, send)
            return
        headers = list(scope.get("headers", ()))

        def values(name: bytes) -> list[bytes]:
            return [v for k, v in headers if k == name]

        async def refuse(status: int, text: str) -> None:
            if kind == "http":
                await _respond(send, status, text)
            else:
                await send({"type": "websocket.close", "code": 4000 + status, "reason": text})

        keys = values(b"x-toolpass-key")
        if key is not None and not (len(keys) == 1 and hmac.compare_digest(keys[0], key.encode())):
            await refuse(403, "toolpass: this server needs the X-Toolpass-Key header, and this request's is missing or wrong")
            return
        users = values(b"x-toolpass-user")
        if not users:
            # Under no session: one the process made current must not serve a request that names nobody.
            token = _current.set(None)
            try:
                await app(scope, receive, send)
            finally:
                _current.reset(token)
            return
        if len(users) > 1:
            await refuse(400, "toolpass: the request carries more than one X-Toolpass-User header")
            return
        user = _text(users[0])
        if not user:
            await refuse(400, "toolpass: the X-Toolpass-User header is empty")
            return
        group_headers = values(b"x-toolpass-groups")
        if len(group_headers) > 1:
            await refuse(400, "toolpass: the request carries more than one X-Toolpass-Groups header")
            return
        groups = tuple(g.strip() for g in _text(group_headers[0]).split(",") if g.strip()) if group_headers else None
        named, mcp = values(b"x-toolpass-session"), values(b"mcp-session-id")
        if len(named) > 1:
            await refuse(400, "toolpass: the request carries more than one X-Toolpass-Session header")
            return
        sid = b"named:" + named[0] if named and named[0].strip() else b"mcp:" + mcp[0] if len(mcp) == 1 else None
        session = session_for(user, groups, sid)
        try:
            with session.active():
                await app(scope, receive, send)
        finally:
            if sid is not None and not named and kind == "http" and scope.get("method") == "DELETE":
                with lock:
                    remembered.pop((user, groups, sid), None)

    return wrapped


def _text(value: bytes) -> str:
    try:
        return value.decode("utf-8").strip()
    except UnicodeDecodeError:
        return ""


async def _respond(send: _Send, status: int, text: str) -> None:
    body = text.encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", b"text/plain; charset=utf-8"), (b"content-length", str(len(body)).encode())],
        }
    )
    await send({"type": "http.response.body", "body": body})

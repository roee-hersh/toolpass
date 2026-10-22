"""The call that is running right now, for the hook under the SDKs.

While a secured tool's body runs, ``current_call()`` answers which call it
is: the toolkit, the spec, the session and what the body has done so far.
The HTTP hook (``_wire``) reads it to attribute each outbound request to
the call and to record the action it turned out to be.

``as_user()`` marks a block of the body whose requests are checked against
the session's user; ``as_agent()`` marks a block acting on the agent's own
authority, whose requests are not. Outside both, the toolkit's ``act_as``
setting decides: ``agent`` (the default) checks only the requests inside
``as_user()``, ``user`` every request but those inside ``as_agent()``. Either way the requests
are still classified, limited, scoped and audited.
"""

from __future__ import annotations

import contextlib
import contextvars
import dataclasses
import logging
import threading
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from toolpass._session import Session

log = logging.getLogger("toolpass.toolkit")

__all__ = ["CallContext", "ObservedAction", "as_agent", "as_user", "checks_user", "current_call"]


@dataclasses.dataclass
class ObservedAction:
    """One request a tool's body made, as toolpass understood it.

    ``action`` is ``<integration>:<action>`` (``github:pr.create``) or None
    when the request needed no permission check; ``resource`` is the
    engine's resource (``repo:acme/x``). ``outcome`` is ``allowed``
    or ``refused``; ``code`` and ``reason`` say why when it is
    not allowed. ``note`` marks a request toolpass could not map
    (``unknown_host``, ``unmapped``)."""

    method: str
    host: str
    path: str
    effect: str
    connection: str | None = None
    integration: str | None = None
    action: str | None = None
    resource: str | None = None
    as_agent: bool = False
    sends_out: bool = False
    untrusted_output: bool = False
    outcome: str = "allowed"
    code: str | None = None
    reason: str | None = None
    note: str | None = None
    # The path variables and body fields the route read, for the recorder.
    fields: dict[str, str] = dataclasses.field(default_factory=dict, repr=False, compare=False)

    @property
    def name(self) -> str | None:
        """``integration:action``, or None."""
        if self.action is None or self.integration is None:
            return None
        return f"{self.integration}:{self.action}"

    def to_dict(self) -> dict[str, Any]:
        out = {k: v for k, v in dataclasses.asdict(self).items() if v is not None and v is not False and k != "fields"}
        if self.name is not None:
            out["action"] = self.name
        return out


@dataclasses.dataclass
class CallContext:
    """What the hook needs to know about the running call."""

    toolkit: Any
    spec: Any
    session: Session
    call: Any
    observed: list[ObservedAction]
    entitlements: Any = None
    strict: bool = False
    # The toolkit's act_as when the call started: a configure() mid-call does not change it.
    act_as: Literal["user", "agent"] = "agent"
    # Set by the hook when it refuses a request; decides the call's outcome.
    refusal: BaseException | None = None
    # Actions the pre-body checks already allowed for this call: (name, resource).
    preallowed: set[tuple[str, str]] = dataclasses.field(default_factory=set)
    # Requests refused once; a retry of the same request gets the same answer.
    refused: dict[str, BaseException] = dataclasses.field(default_factory=dict)
    lock: threading.Lock = dataclasses.field(default_factory=threading.Lock)


_current: contextvars.ContextVar[CallContext | None] = contextvars.ContextVar("toolpass_call", default=None)
# True inside as_user(), False inside as_agent(), None outside both.
_as_user: contextvars.ContextVar[bool | None] = contextvars.ContextVar("toolpass_as_user", default=None)


def current_call() -> CallContext | None:
    """The secured-tool call whose body is running in this context, or None."""
    return _current.get()


def checks_user(act_as: Literal["user", "agent"]) -> bool:
    """Whether a request made here is checked against the session's user,
    for a toolkit whose ``act_as`` setting is ``act_as``. The innermost
    ``as_user()`` or ``as_agent()`` block decides."""
    marked = _as_user.get()
    return act_as == "user" if marked is None else marked


@contextlib.contextmanager
def _acting(user: bool, name: str) -> Iterator[None]:
    if _current.get() is None:
        log.warning("toolpass: %s() used outside a secured tool's body; it has no effect there", name)
    token = _as_user.set(user)
    try:
        yield
    finally:
        _as_user.reset(token)


@contextlib.contextmanager
def as_agent() -> Iterator[None]:
    """Run the block on the agent's own authority.

    Inside a secured tool's body, requests made in the block are not checked
    against the session's user; scope, limits, the untrusted-input and
    exfiltration checks, entitlements and the audit still apply, and each
    action is recorded with ``as_agent``. With ``act_as="agent"`` (the
    default) every request outside ``as_user()`` already is, and the block
    changes nothing unless it sits inside ``as_user()``. Outside a tool body
    the block changes nothing and a warning is logged.

        toolpass.configure(permissions=tp, act_as="user")

        with toolpass.as_agent():
            files = gh.get_repo(repo).get_contents(path)   # the agent may read
        gh.get_repo(repo).create_pull(...)                  # checked against the user
    """
    with _acting(False, "as_agent"):
        yield


@contextlib.contextmanager
def as_user() -> Iterator[None]:
    """Check the requests made in the block against the session's user.

    With ``act_as="agent"`` (the default) the block is how a tool says which
    requests the user must be allowed to make; with ``act_as="user"`` every
    request already is, and the block changes nothing unless it sits inside
    ``as_agent()``. Outside a tool body the block changes nothing and a
    warning is logged.

        r = gh.get_repo(repo)                                # the agent's own authority
        files = r.get_contents(path)
        with toolpass.as_user():
            r.create_pull(...)                               # checked against the user
    """
    with _acting(True, "as_user"):
        yield


@contextlib.contextmanager
def running(ctx: CallContext) -> Iterator[None]:
    """Make ``ctx`` the current call for the block (the toolkit, around the body)."""
    token = _current.set(ctx)
    as_user_token = _as_user.set(None)  # a nested tool starts outside any as_agent() or as_user() block
    try:
        yield
    finally:
        _as_user.reset(as_user_token)
        _current.reset(token)

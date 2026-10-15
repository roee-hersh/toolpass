"""The call that is running right now, for the hook under the SDKs.

While a secured tool's body runs, ``current_call()`` answers which call it
is: the toolkit, the spec, the session and what the body has done so far.
The HTTP hook (``_wire``) reads it to attribute each outbound request to
the call and to record the action it turned out to be.

``as_agent()`` marks a block of the body as acting on the agent's own
authority: the requests made inside it are still classified, limited,
scoped and audited, but not checked against the session's user.
"""

from __future__ import annotations

import contextlib
import contextvars
import dataclasses
import logging
import threading
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from toolpass._session import Session

log = logging.getLogger("toolpass.toolkit")

__all__ = ["CallContext", "ObservedAction", "as_agent", "current_call", "in_agent_block"]


@dataclasses.dataclass
class ObservedAction:
    """One request a tool's body made, as toolpass understood it.

    ``action`` is ``<integration>:<action>`` (``github:pr.create``) or None
    when the request needed no permission check; ``resource`` is the
    engine's resource (``repo:acme/x``). ``outcome`` is ``allowed``,
    ``refused`` or ``pending``; ``code`` and ``reason`` say why when it is
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
    # Set by the hook when it refuses a request; decides the call's outcome.
    refusal: BaseException | None = None
    # Actions the pre-body checks already allowed for this call: (name, resource).
    preallowed: set[tuple[str, str]] = dataclasses.field(default_factory=set)
    # Approvals already granted before the body: the reasons they covered.
    preapproved: set[str] = dataclasses.field(default_factory=set)
    # Requests refused once; a retry of the same request gets the same answer.
    refused: dict[str, BaseException] = dataclasses.field(default_factory=dict)
    lock: threading.Lock = dataclasses.field(default_factory=threading.Lock)


_current: contextvars.ContextVar[CallContext | None] = contextvars.ContextVar("toolpass_call", default=None)
_agent_depth: contextvars.ContextVar[int] = contextvars.ContextVar("toolpass_as_agent", default=0)

# How many tool bodies are running anywhere in the process, and how many of
# them under a strict toolkit. A request that arrives with no call context
# while a strict body runs is refused: a thread that lost the context.
_bodies = 0
_strict_bodies = 0
_bodies_lock = threading.Lock()


def current_call() -> CallContext | None:
    """The secured-tool call whose body is running in this context, or None."""
    return _current.get()


def in_agent_block() -> bool:
    return _agent_depth.get() > 0


@contextlib.contextmanager
def as_agent() -> Iterator[None]:
    """Run the block on the agent's own authority.

    Inside a secured tool's body, requests made in the block are not checked
    against the session's user; scope, limits, the untrusted-input and
    exfiltration checks, entitlements and the audit still apply, and each
    action is recorded with ``as_agent``. Outside a tool body the block
    changes nothing and a warning is logged.

        with toolpass.as_agent():
            files = gh.get_repo(repo).get_contents(path)   # the agent may read
        gh.get_repo(repo).create_pull(...)                  # checked against the user
    """
    if _current.get() is None:
        log.warning("toolpass: as_agent() used outside a secured tool's body; it has no effect there")
    token = _agent_depth.set(_agent_depth.get() + 1)
    try:
        yield
    finally:
        _agent_depth.reset(token)


@contextlib.contextmanager
def running(ctx: CallContext) -> Iterator[None]:
    """Make ``ctx`` the current call for the block (the toolkit, around the body)."""
    global _bodies, _strict_bodies
    token = _current.set(ctx)
    agent_token = _agent_depth.set(0)  # a nested tool starts outside any as_agent() block
    with _bodies_lock:
        _bodies += 1
        if ctx.strict:
            _strict_bodies += 1
    try:
        yield
    finally:
        with _bodies_lock:
            _bodies -= 1
            if ctx.strict:
                _strict_bodies -= 1
        _agent_depth.reset(agent_token)
        _current.reset(token)


def strict_body_running() -> bool:
    """True while any strict toolkit's tool body runs somewhere in the process."""
    with _bodies_lock:
        return _strict_bodies > 0

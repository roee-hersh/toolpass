"""``toolpass demo``: an agent with a Jira-like tool, two users and one guard,
on the live page. No network, no model: a script plays the agent, so the
run is the same every time, and the guard is the same code that runs under
a real agent.

    toolpass demo            # opens nothing; prints the page's address
    toolpass demo -open      # opens the page in your browser
    toolpass demo -auto      # approves the waiting call itself, for a terminal with nobody at it
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

from toolpass._api import Toolpass
from toolpass._approval import ApprovalQueue, ApprovalRequest
from toolpass._permissions import permission_check
from toolpass._session import Session
from toolpass._toolkit import ApprovalPending, Call, Toolkit, ToolRefused
from toolpass._watch import watch

DANA, ADMIN = "dana@example.com", "admin@example.com"

ISSUES = {
    "PAY-123": "duplicate of PAY-120: checkout timeout on retry; reporter dana; open",
    "PAY-120": "checkout timeout on retry; reporter omer; in progress",
}


def build(kit: Toolkit, tp: Toolpass) -> dict[str, Callable[..., Any]]:
    """The agent's two tools. ``delete_issue`` is the one that matters: the
    permission engine answers whether the session's user may delete issues
    in the Jira connection, and a person confirms the exact call."""

    @kit.secured_tool(effect="read")
    def get_issue(key: str) -> str:
        """Read a Jira issue."""
        return f"{key}: {ISSUES.get(key, 'no such issue')}"

    @kit.secured_tool(
        effect="destructive",
        scope={"key": "PAY-*"},
        authorize=permission_check(tp, "jira-demo", "thing.write", "thing:{key}"),
        approve=True,
        preview=lambda call: f"delete {call.arguments['key']} ({ISSUES.get(call.arguments['key'], 'unknown')})",
    )
    def delete_issue(key: str) -> str:
        """Delete a Jira issue."""
        ISSUES.pop(key, None)
        return f"deleted {key}"

    return {"get_issue": get_issue, "delete_issue": delete_issue}


def run(
    say: Callable[[str], None],
    *,
    listen: str = "127.0.0.1:0",
    auto: bool = False,
    open_browser: bool = False,
    timeout: float = 120.0,
    hold: Callable[[], None] | None = None,
) -> list[tuple[str, str]]:
    """Run the demo; returns (step, outcome) pairs. ``auto`` approves the
    waiting call from inside the process; otherwise a person approves it on
    the page, within ``timeout`` seconds. ``hold`` is called at the end with
    the page still up (a prompt that waits for Enter), when given."""
    tp = Toolpass(connections=[{"id": "jira-demo", "integration": "fake", "users": DANA, "admins": ADMIN}])
    approvals = ApprovalQueue()
    kit = Toolkit(approver=approvals, audit=None, limits={"destructive": 3})
    tools = build(kit, tp)
    outcomes: list[tuple[str, str]] = []
    if auto:
        approvals.on_request(lambda request: _approve_soon(approvals, request))

    def step(label: str, fn: Callable[..., Any], **kwargs: Any) -> Any:
        try:
            result = fn(**kwargs)
            outcome = "ran"
            say(f"[ran]     {label}\n          -> {result}")
        except ApprovalPending as e:
            result, outcome = e, "approval_pending"
            say(f"[waiting] {label}\n          -> {e.reason}")
        except ToolRefused as e:
            result, outcome = e, e.code
            say(f"[refused] {label}\n          -> {e.code}: {e.reason}")
        outcomes.append((label, outcome))
        return result

    with watch(kit, listen=listen, open_browser=open_browser) as page:
        say("toolpass demo: an agent with a Jira tool, two users, one guard. No network, no model.")
        say(f"watch it live: {page.url}\n")
        say(f'== {DANA} asks: "PAY-123 is a duplicate I filed by mistake, delete it" ==\n')
        with Session(DANA).active():
            step("get_issue(PAY-123)", tools["get_issue"], key="PAY-123")
            step("delete_issue(PAY-123)", tools["delete_issue"], key="PAY-123")
        say(f"\n== {ADMIN} asks the same ==\n")
        with Session(ADMIN).active():
            pending = step("delete_issue(PAY-123)", tools["delete_issue"], key="PAY-123")
            if isinstance(pending, ApprovalPending):
                if auto:
                    say("          (approved by -auto; a person would press Approve on the page)")
                else:
                    say(f"          open the page and press Approve (waiting up to {timeout:g}s; -auto approves by itself)")
                deadline = time.monotonic() + timeout
                while pending.request.id in {r.id for r in approvals.pending()} and time.monotonic() < deadline:
                    time.sleep(0.2)
                if pending.request.id in {r.id for r in approvals.pending()}:
                    say("[timeout] nobody approved it on the page; run with -auto, or -timeout 10m to wait longer")
                    outcomes.append(("delete_issue(PAY-123) retried", "timeout"))
                else:
                    step("delete_issue(PAY-123) retried, after a person decided", tools["delete_issue"], key="PAY-123")
            step("delete_issue(OPS-9), another project", tools["delete_issue"], key="OPS-9")
        say(f"\n{len(outcomes)} calls, each on the page with who asked and why it was decided.")
        if hold is not None:
            hold()
    return outcomes


def _approve_soon(approvals: ApprovalQueue, request: ApprovalRequest) -> None:
    """Approve a request shortly after it was made, from another thread, as
    a person on the page would."""

    def approve() -> None:
        time.sleep(0.2)
        try:
            approvals.approve(request.id, by="auto")
        except KeyError:
            pass

    threading.Thread(target=approve, name="toolpass-demo-approve", daemon=True).start()


__all__ = ["build", "run"]

_ = Call  # re-exported for type checkers reading the preview's argument

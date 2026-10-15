"""toolpass: a pass for every tool call. Secure-by-default tools for AI agents.

    import toolpass
    from toolpass import ApprovalQueue, Session, Toolpass, permission_check, secured_tool

    tp = Toolpass.from_config("toolpass.yaml")            # permission checks, live
    toolpass.configure(approver=ApprovalQueue(), credentials={"github-bot": token})

    @secured_tool(effect="write", scope={"repo": "acme/gitops-*"}, approve=True, credential="github-bot",
                  authorize=permission_check(tp, "github-acme", "pr.create", "repo:{repo}"))
    def open_pr(repo: str, title: str, *, credential: str) -> str: ...

    with Session("dana@example.com").active():
        agent.run(prompt)

``secured_tool`` declares a tool and runs the checks around every call;
``Toolkit`` holds a separate set of settings when one program needs two.
``Toolpass`` answers "may this user do this?" by asking the system that owns
the resource, in-process or against a toolpass server; ``guarded`` and the
framework adapters use it on their own.
"""

from toolpass._api import (
    ALLOW,
    DENY,
    UNKNOWN,
    Decision,
    GroupsSource,
    PermissionDenied,
    Toolpass,
    UserSource,
    current,
    guarded,
)
from toolpass._approval import ApprovalQueue, ApprovalRequest, Approver, approve_all
from toolpass._audit import AuditEvent, AuditSink, log_audit
from toolpass._checks import ScopeRule, Validator
from toolpass._context import ObservedAction, as_agent, current_call
from toolpass._default import configure, default_toolkit, secured_tool
from toolpass._entitlements import ActionRecorder, Entitlement, Entitlements, ToolEntitlements
from toolpass._permissions import permission_check
from toolpass._session import Session, current_session
from toolpass._toolkit import (
    EFFECTS,
    ApprovalPending,
    AuthDecision,
    Authorizer,
    Call,
    CredentialSource,
    Effect,
    ToolError,
    Toolkit,
    ToolRefused,
    ToolSpec,
    fence,
    spec_of,
)
from toolpass._version import __version__
from toolpass._watch import Watch, watch
from toolpass._wire import ActionRefused
from toolpass._wire import install as intercept
from toolpass.core.secret import env, file, literal

__all__ = [
    "ALLOW",
    "DENY",
    "EFFECTS",
    "UNKNOWN",
    "ActionRecorder",
    "ActionRefused",
    "ApprovalPending",
    "ApprovalQueue",
    "ApprovalRequest",
    "Approver",
    "AuditEvent",
    "AuditSink",
    "AuthDecision",
    "Authorizer",
    "Call",
    "CredentialSource",
    "Decision",
    "Effect",
    "Entitlement",
    "Entitlements",
    "GroupsSource",
    "ObservedAction",
    "PermissionDenied",
    "ScopeRule",
    "Session",
    "ToolEntitlements",
    "ToolError",
    "ToolRefused",
    "ToolSpec",
    "Toolkit",
    "Toolpass",
    "UserSource",
    "Validator",
    "Watch",
    "__version__",
    "approve_all",
    "as_agent",
    "configure",
    "current",
    "current_call",
    "current_session",
    "default_toolkit",
    "env",
    "fence",
    "file",
    "guarded",
    "intercept",
    "literal",
    "log_audit",
    "permission_check",
    "secured_tool",
    "spec_of",
    "watch",
]

"""agent-authorizer: authorizes what an AI agent does against the permissions of the user it acts for.

    import agent_authorizer
    from agent_authorizer import Session, AgentAuthorizer, permission_check

    tp = AgentAuthorizer.from_config("agent-authorizer.yaml")            # permission checks, live
    agent_authorizer.configure(credentials={"github-bot": token})

    @agent_authorizer.tool(effect="write", scope={"repo": "acme/gitops-*"}, credential="github-bot",
                  authorize=permission_check(tp, "github-acme", "pr.create", "repo:{repo}"))
    def open_pr(repo: str, title: str, *, credential: str) -> str: ...

    with Session("dana@example.com").active():
        agent.run(prompt)

``tool`` declares a tool and runs the checks around every call;
``Toolkit`` holds a separate set of settings when one program needs two.
``AgentAuthorizer`` answers "may this user do this?" by asking the system that owns
the resource, in-process or against an agent-authorizer server; ``guarded`` and the
framework adapters use it on their own.
"""

from agent_authorizer._api import (
    ALLOW,
    DENY,
    UNKNOWN,
    AgentAuthorizer,
    Decision,
    GroupsSource,
    PermissionDenied,
    UserSource,
    current,
    guarded,
)
from agent_authorizer._audit import AuditEvent, AuditSink, log_audit
from agent_authorizer._checks import ScopeRule, Validator
from agent_authorizer._context import ObservedAction, as_agent, as_user, current_call
from agent_authorizer._default import configure, default_toolkit, secured_tool, tool
from agent_authorizer._entitlements import ActionRecorder, Entitlement, Entitlements, ToolEntitlements
from agent_authorizer._fence import FENCE_END, FENCE_START, Fenced, fence, neutralize
from agent_authorizer._permissions import permission_check
from agent_authorizer._session import GROUPS_HEADER, KEY_HEADER, SESSION_HEADER, TOKEN_HEADER, USER_HEADER, Session, current_session, session_from_header
from agent_authorizer._toolkit import (
    EFFECTS,
    AuthDecision,
    Authorizer,
    Call,
    CredentialSource,
    Effect,
    ToolError,
    Toolkit,
    ToolRefused,
    ToolSpec,
    spec_of,
)
from agent_authorizer._version import __version__
from agent_authorizer._watch import Watch, watch
from agent_authorizer._wire import ActionRefused
from agent_authorizer._wire import install as intercept
from agent_authorizer.core.secret import env, file, literal

__all__ = [
    "ALLOW",
    "DENY",
    "EFFECTS",
    "FENCE_END",
    "FENCE_START",
    "GROUPS_HEADER",
    "KEY_HEADER",
    "SESSION_HEADER",
    "TOKEN_HEADER",
    "UNKNOWN",
    "USER_HEADER",
    "ActionRecorder",
    "ActionRefused",
    "AgentAuthorizer",
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
    "Fenced",
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
    "UserSource",
    "Validator",
    "Watch",
    "__version__",
    "as_agent",
    "as_user",
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
    "neutralize",
    "permission_check",
    "secured_tool",
    "session_from_header",
    "spec_of",
    "tool",
    "watch",
]

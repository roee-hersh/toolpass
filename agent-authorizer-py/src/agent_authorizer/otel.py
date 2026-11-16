"""Export each run of an agent-authorizer tool as an OpenTelemetry span, into the
tracing your team already has (Grafana Tempo, Datadog, Honeycomb, Jaeger).

    pip install "agent-authorizer[otel]"

    import agent_authorizer
    from agent_authorizer.otel import audit_sink

    agent_authorizer.configure(audit=audit_sink())        # the default toolkit
    tools = Toolkit(audit=audit_sink())           # or one toolkit

One span per run, ``agent_authorizer.tool <name>``, with the user, the outcome and why
as attributes, timed from the run's own start and duration. Each request the
body made to GitHub, Jira, ... is a span event, ``agent_authorizer.request``, with the
system, the action, the resource and whether it was allowed. A refused or
failed run sets the span's status to ERROR. Arguments are not exported: they
can carry what the user typed.

The sink uses the global tracer provider the application configured (the
``opentelemetry-sdk`` with an exporter); without one, the spans go nowhere,
which is OpenTelemetry's own default. Pass ``tracer`` to use another. ``then``
is a sink to hand each event on to as well (``log_audit``, the default
JSON log line).
"""

from __future__ import annotations

import datetime
from collections.abc import Mapping
from typing import Any

from agent_authorizer._audit import AuditEvent, AuditSink

try:
    from opentelemetry import trace
    from opentelemetry.trace import SpanKind, Status, StatusCode
except ImportError as e:  # pragma: no cover - exercised only without the extra
    raise ImportError('agent_authorizer.otel needs the OpenTelemetry API: pip install "agent-authorizer[otel]"') from e

__all__ = ["audit_sink"]

_ATTRS = {
    "user": "agent_authorizer.user",
    "session": "agent_authorizer.session",
    "effect": "agent_authorizer.effect",
    "code": "agent_authorizer.code",
    "reason": "agent_authorizer.reason",
}
_REQUEST_ATTRS = ("integration", "connection", "action", "resource", "effect", "method", "host", "path", "outcome", "code", "reason")


def _ns(iso: str) -> int | None:
    try:
        return int(datetime.datetime.fromisoformat(iso).timestamp() * 1e9)
    except ValueError:
        return None


def _str(value: Any) -> str:
    return value if isinstance(value, str) else repr(value)


def audit_sink(tracer: Any = None, *, then: AuditSink | None = None) -> AuditSink:
    """An audit sink that records each run as a span; see the module docstring."""
    tr = tracer if tracer is not None else trace.get_tracer("agent-authorizer")

    def sink(event: AuditEvent) -> None:
        start = _ns(event.time)
        end = start + int((event.duration_ms or 0.0) * 1e6) if start is not None else None
        attrs: dict[str, Any] = {"agent_authorizer.tool": event.tool, "agent_authorizer.outcome": event.outcome}
        for field, key in _ATTRS.items():
            value = getattr(event, field)
            if value is not None:
                attrs[key] = _str(value)
        span = tr.start_span(f"agent_authorizer.tool {event.tool}", kind=SpanKind.INTERNAL, attributes=attrs, start_time=start)
        try:
            for action in event.actions or ():
                if isinstance(action, Mapping):
                    span.add_event(
                        "agent_authorizer.request",
                        {f"agent_authorizer.{k}": _str(action[k]) for k in _REQUEST_ATTRS if action.get(k) is not None},
                        timestamp=end,
                    )
            if event.outcome == "ran":
                span.set_status(Status(StatusCode.OK))
            else:
                span.set_status(Status(StatusCode.ERROR, f"{event.outcome}: {event.reason or event.code or ''}".rstrip(": ")))
        finally:
            span.end(end_time=end)
        if then is not None:
            then(event)

    return sink

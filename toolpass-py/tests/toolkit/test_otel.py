"""toolpass.otel: each run of a secured tool as an OpenTelemetry span, its
requests as span events, into the application's own tracer provider."""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("opentelemetry.sdk.trace")

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from toolpass import Session, Toolkit
from toolpass._audit import AuditEvent
from toolpass.otel import audit_sink


def _tracer() -> tuple[Any, InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider.get_tracer("test"), exporter


def test_a_run_is_a_span_with_its_requests_as_events() -> None:
    tracer, exporter = _tracer()
    handed: list[AuditEvent] = []
    tools = Toolkit(audit=audit_sink(tracer, then=handed.append))

    @tools.tool(effect="read")
    def lookup(key: str) -> str:
        return "found " + key

    with Session("dana@acme.dev", groups=["eng"]).active():
        lookup(key="SHOP-1")
    [span] = exporter.get_finished_spans()
    assert span.name == "toolpass.tool lookup"
    assert span.attributes["toolpass.tool"] == "lookup"
    assert span.attributes["toolpass.user"] == "dana@acme.dev"
    assert span.attributes["toolpass.outcome"] == "ran"
    assert span.status.status_code is StatusCode.OK
    assert "toolpass.arguments" not in span.attributes and "SHOP-1" not in str(span.attributes), "arguments are not exported"
    assert span.end_time >= span.start_time
    assert [e.tool for e in handed] == ["lookup"], "the event went on to the next sink"


def test_a_refused_run_is_an_error_span() -> None:
    tracer, exporter = _tracer()
    event = AuditEvent(
        time="2026-10-16T12:00:00.000+00:00",
        tool="open_pr",
        effect="write",
        outcome="refused",
        code="not_authorized",
        reason="sam may not pr.create on repo:acme/shop",
        user="sam@acme.dev",
        duration_ms=47.0,
        actions=[
            {"integration": "github", "action": "repo.read", "method": "GET", "path": "/api/v3/repos/acme/shop", "outcome": "allowed"},
            {"integration": "github", "action": "pr.create", "resource": "repo:acme/shop", "outcome": "refused", "reason": "sam has read"},
        ],
    )
    audit_sink(tracer)(event)
    [span] = exporter.get_finished_spans()
    assert span.status.status_code is StatusCode.ERROR and "not pr.create" in span.status.description
    assert span.attributes["toolpass.code"] == "not_authorized"
    assert span.end_time - span.start_time == 47_000_000, "timed from the run's own start and duration"
    assert [e.name for e in span.events] == ["toolpass.request", "toolpass.request"]
    assert span.events[1].attributes["toolpass.action"] == "pr.create"
    assert span.events[1].attributes["toolpass.outcome"] == "refused"
    assert span.events[1].attributes["toolpass.reason"] == "sam has read"
    assert "toolpass.resource" not in span.events[0].attributes, "only what the request carried"


def test_without_a_provider_nothing_breaks() -> None:
    tools = Toolkit(audit=audit_sink())  # the API's no-op tracer

    @tools.tool(effect="read")
    def lookup(key: str) -> str:
        return key

    with Session("dana@acme.dev").active():
        assert lookup(key="x") == "x"

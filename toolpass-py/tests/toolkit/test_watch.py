"""The live page: every call shows up, a waiting call is decided from the
page, and nothing is readable or decidable without the token, from another
origin, or off the loopback interface."""

from __future__ import annotations

import http.client
import json
import re
import threading
from collections.abc import Iterator
from typing import Any

import pytest

import toolpass
from toolpass import ApprovalPending, ApprovalQueue, AuditEvent, Session, Toolkit, ToolRefused, Watch, watch

from .conftest import Events


def _parts(url: str) -> tuple[str, int, str]:
    m = re.fullmatch(r"http://([^:]+):(\d+)(/t/[^/]+/)", url)
    assert m, url
    return m.group(1), int(m.group(2)), m.group(3)


def _request(page: Watch, method: str, path: str, *, host: str | None = None, body: bytes | None = None) -> tuple[int, dict[str, str], bytes]:
    h, port, prefix = _parts(page.url)
    conn = http.client.HTTPConnection(h, port, timeout=5)
    headers = {"Host": host} if host is not None else {}
    if body is not None:
        headers["Content-Length"] = str(len(body))
    conn.request(method, prefix + path if not path.startswith("/") else path, body=body, headers=headers)
    r = conn.getresponse()
    data = r.read()
    conn.close()
    return r.status, {k.lower(): v for k, v in r.getheaders()}, data


def _events(page: Watch, since: int = 0) -> dict[str, Any]:
    status, _, data = _request(page, "GET", f"events?since={since}")
    assert status == 200
    out: dict[str, Any] = json.loads(data)
    return out


@pytest.fixture
def kit(events: Events) -> Iterator[tuple[Toolkit, Watch]]:
    toolkit = Toolkit(approver=ApprovalQueue(), audit=events)
    page = watch(toolkit)
    yield toolkit, page
    page.close()


def test_calls_show_up_and_a_waiting_call_is_decided_from_the_page(kit: tuple[Toolkit, Watch], events: Events, dana: Session) -> None:
    toolkit, page = kit

    @toolkit.secured_tool(effect="destructive", scope={"key": "PAY-*"}, approve=True, preview=lambda c: f"delete {c.arguments['key']}")
    def delete_issue(key: str) -> str:
        return f"deleted {key}"

    with pytest.raises(ToolRefused) as refused:
        delete_issue(key="OPS-1")
    assert refused.value.code == "out_of_scope"
    with pytest.raises(ApprovalPending) as pending:
        delete_issue(key="PAY-1")
    approval = pending.value.request.id

    d = _events(page)
    assert [e["outcome"] for e in d["events"]] == ["refused", "pending"]
    assert d["events"][0]["code"] == "out_of_scope" and d["events"][0]["user"] == "dana@example.com"
    assert d["events"][1]["approval"] == approval and "delete PAY-1" in d["events"][1]["describe"]
    assert d["pending"] == [approval] and d["decidable"] is True
    assert len(events) == 2, "the previous audit sink still receives every event"

    status, _, data = _request(page, "POST", f"approve/{approval}", body=b"")
    assert status == 200 and json.loads(data) == {"decided": True}
    assert delete_issue(key="PAY-1") == "deleted PAY-1"
    d = _events(page, since=d["next"])
    assert [e["outcome"] for e in d["events"]] == ["ran"] and d["pending"] == []
    status, _, _ = _request(page, "POST", f"approve/{approval}", body=b"")
    assert status == 409, "an approval that is no longer pending cannot be decided again"


def test_reject_from_the_page(kit: tuple[Toolkit, Watch], dana: Session) -> None:
    toolkit, page = kit

    @toolkit.secured_tool(effect="write", approve=True)
    def post(text: str) -> str:
        return "posted"

    with pytest.raises(ApprovalPending) as pending:
        post(text="hi")
    status, _, _ = _request(page, "POST", f"reject/{pending.value.request.id}", body=b"")
    assert status == 200
    with pytest.raises(ToolRefused) as refused:
        post(text="hi")
    assert refused.value.code == "approval_rejected"


def test_the_page_and_its_assets_are_served_with_headers_that_keep_data_out_of_other_origins(kit: tuple[Toolkit, Watch]) -> None:
    _, page = kit
    status, headers, body = _request(page, "GET", "")
    assert status == 200 and b"<!doctype html>" in body and b"watch.js" in body
    assert headers["content-security-policy"].startswith("default-src 'none'")
    assert headers["x-frame-options"] == "DENY" and headers["cache-control"] == "no-store"
    for asset, kind in (("watch.js", "text/javascript"), ("watch.css", "text/css")):
        status, headers, _ = _request(page, "GET", asset)
        assert status == 200 and headers["content-type"].startswith(kind) and headers["x-content-type-options"] == "nosniff"


def test_nothing_without_the_token_or_from_another_host(kit: tuple[Toolkit, Watch]) -> None:
    _, page = kit
    _, port, _ = _parts(page.url)
    assert _request(page, "GET", "/t/wrong-token/events")[0] == 404
    assert _request(page, "GET", "/")[0] == 404
    assert _request(page, "GET", "/events")[0] == 404
    assert _request(page, "POST", "/t/wrong-token/approve/x", body=b"")[0] == 404
    # DNS rebinding: a page elsewhere resolves a name to 127.0.0.1 and sends its own Host.
    assert _request(page, "GET", "events", host="evil.example")[0] == 403
    assert _request(page, "GET", "events", host=f"127.0.0.1:{port}")[0] == 200
    assert _request(page, "GET", "events", host="localhost")[0] == 200
    assert _request(page, "GET", "events", host="[::1]:80")[0] == 200
    assert _request(page, "GET", "unknown")[0] == 404
    assert _request(page, "GET", "events", body=b"x" * 70000)[0] == 413


def test_loopback_only() -> None:
    with pytest.raises(ValueError, match="loopback"):
        Watch(Toolkit(audit=None), listen="0.0.0.0:0")
    with pytest.raises(ValueError, match="loopback"):
        Watch(Toolkit(audit=None), listen="192.168.1.5:8080")


def test_default_toolkit_and_a_toolkit_without_an_approver_get_a_queue(dana: Session) -> None:
    toolpass.configure(audit=None)

    @toolpass.secured_tool(effect="write", approve=True)
    def post(text: str) -> str:
        return "posted"

    with pytest.raises(ToolRefused) as before:
        post(text="x")
    assert before.value.code == "approval_unavailable"
    with watch() as page:
        assert toolpass.default_toolkit().approver is page.queue and isinstance(page.queue, ApprovalQueue)
        with pytest.raises(ApprovalPending):
            post(text="x")
        assert len(page.pending()) == 1


def test_close_detaches_and_a_custom_approver_is_shown_not_decided(events: Events, dana: Session) -> None:
    toolkit = Toolkit(approver=lambda request: None, audit=events)
    page = watch(toolkit)

    @toolkit.secured_tool(effect="write", approve=True)
    def post(text: str) -> str:
        return "posted"

    with pytest.raises(ApprovalPending):
        post(text="x")
    d = _events(page)
    assert d["events"][0]["outcome"] == "pending" and d["pending"] == [] and d["decidable"] is False
    assert page.decide("anything", True) is False
    page.close()
    assert toolkit.audit is events, "the previous sink is given back"
    page.close()  # idempotent
    with pytest.raises(ApprovalPending):
        post(text="y")
    assert len(events) == 2 and page.events()[1] == 1, "nothing recorded after close"


def test_events_are_kept_to_a_window(dana: Session) -> None:
    toolkit = Toolkit(audit=None)
    page = Watch(toolkit, keep=3)
    try:

        @toolkit.secured_tool(effect="read")
        def read() -> str:
            return "ok"

        for _ in range(5):
            read()
        rows, nxt = page.events()
        assert nxt == 5 and [r["seq"] for r in rows] == [3, 4, 5]
    finally:
        page.close()


def test_many_readers_at_once(kit: tuple[Toolkit, Watch], dana: Session) -> None:
    toolkit, page = kit

    @toolkit.secured_tool(effect="read")
    def read() -> str:
        return "ok"

    read()
    results: list[int] = []

    def hit() -> None:
        results.append(_request(page, "GET", "events")[0])

    threads = [threading.Thread(target=hit) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results == [200] * 12


def test_audit_sink_failure_does_not_reach_the_page(dana: Session) -> None:
    def broken(event: AuditEvent) -> None:
        raise RuntimeError("sink down")

    toolkit = Toolkit(audit=broken)
    with watch(toolkit) as page:

        @toolkit.secured_tool(effect="read")
        def read() -> str:
            return "ok"

        assert read() == "ok"
        assert _events(page)["events"][0]["outcome"] == "ran"

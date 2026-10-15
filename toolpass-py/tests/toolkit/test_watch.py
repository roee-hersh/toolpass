"""The live page: every call shows up, the page changes nothing, and
nothing is readable without the token, from another origin, or off the
loopback interface."""

from __future__ import annotations

import http.client
import json
import re
import socket
import threading
from collections.abc import Iterator
from typing import Any

import pytest

from toolpass import AuditEvent, Session, Toolkit, ToolRefused, Watch, watch

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
    toolkit = Toolkit(audit=events)
    page = watch(toolkit)
    yield toolkit, page
    page.close()


def test_calls_show_up_and_the_page_decides_nothing(kit: tuple[Toolkit, Watch], events: Events, dana: Session) -> None:
    toolkit, page = kit

    @toolkit.secured_tool(effect="destructive", scope={"key": "PAY-*"})
    def delete_issue(key: str) -> str:
        return f"deleted {key}"

    with pytest.raises(ToolRefused) as refused:
        delete_issue(key="OPS-1")
    assert refused.value.code == "out_of_scope"
    assert delete_issue(key="PAY-1") == "deleted PAY-1"

    d = _events(page)
    assert [e["outcome"] for e in d["events"]] == ["refused", "ran"]
    assert d["events"][0]["code"] == "out_of_scope" and d["events"][0]["user"] == "dana@example.com"
    assert set(d) == {"events", "next"}
    assert len(events) == 2, "the previous audit sink still receives every event"
    d = _events(page, since=d["next"])
    assert d["events"] == []
    for path in ("approve/x", "reject/x", "events"):
        assert _request(page, "POST", path, body=b"")[0] == 501, "the page has no way to change a call"


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
    assert _request(page, "GET", "/t/caf%C3%A9/events")[0] == 404, "a non-ASCII token segment is a 404, not a dropped connection"
    raw = socket.create_connection(("127.0.0.1", port), timeout=5)  # http.client refuses to send it; the wire does not
    raw.sendall(b"GET /t/caf\xe9/events HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
    assert raw.recv(64).startswith(b"HTTP/1.1 404")
    raw.close()
    assert _request(page, "GET", "/")[0] == 404
    assert _request(page, "GET", "/events")[0] == 404
    assert _request(page, "GET", "/t/wrong-token/watch.js")[0] == 404
    # DNS rebinding: a page elsewhere resolves a name to 127.0.0.1 and sends its own Host.
    assert _request(page, "GET", "events", host="evil.example")[0] == 403
    assert _request(page, "GET", "events", host=f"127.0.0.1:{port}")[0] == 200
    assert _request(page, "GET", "events", host="localhost")[0] == 200
    assert _request(page, "GET", "events", host="[::1]:80")[0] == 200
    assert _request(page, "GET", "unknown")[0] == 404
    assert _request(page, "GET", "events", body=b"x" * 70000)[0] == 413


def test_a_body_not_framed_by_one_plain_length_closes_the_connection(kit: tuple[Toolkit, Watch]) -> None:
    """A chunked body, two lengths or a length that is not digits is not
    read, so none of it is parsed as the next request on the connection."""
    _, port, prefix = _parts(kit[1].url)
    smuggled = f"GET {prefix}events HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n".encode()
    for headers in (b"Transfer-Encoding: chunked", b"Content-Length: 5\r\nContent-Length: 5", b"Content-Length: +5", b"Content-Length: 5_0"):
        raw = socket.create_connection(("127.0.0.1", port), timeout=5)
        raw.sendall(f"GET {prefix}events HTTP/1.1\r\nHost: 127.0.0.1\r\n".encode() + headers + b"\r\n\r\n" + smuggled)
        raw.settimeout(2)
        got = b""
        try:
            while chunk := raw.recv(4096):
                got += chunk
        except TimeoutError:
            pass
        raw.close()
        assert got.startswith(b"HTTP/1.1 413"), (headers, got[:80])
        assert got.count(b"HTTP/1.1 ") == 1, f"the smuggled request was answered: {headers!r}"


def test_open_in_browser_keeps_the_token_off_the_command_line(monkeypatch: pytest.MonkeyPatch) -> None:
    import os
    import pathlib
    import stat

    from toolpass import _watch

    opened: list[str] = []
    monkeypatch.setattr(_watch.webbrowser, "open", lambda url: opened.append(url) or True)
    url = "http://127.0.0.1:1/t/secret-token/"
    path = _watch.open_in_browser(url)
    assert opened == [pathlib.Path(path).as_uri()] and "secret-token" not in opened[0]
    if os.name != "nt":  # Windows has no mode bits; the file is in the user's own temp folder there
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    with open(path, encoding="utf-8") as f:
        assert f'content="0;url={url}"' in f.read()
    os.remove(path)
    os.rmdir(os.path.dirname(path))


def test_loopback_only() -> None:
    with pytest.raises(ValueError, match="loopback"):
        Watch(Toolkit(audit=None), listen="0.0.0.0:0")
    with pytest.raises(ValueError, match="loopback"):
        Watch(Toolkit(audit=None), listen="192.168.1.5:8080")


def test_close_detaches(events: Events, dana: Session) -> None:
    toolkit = Toolkit(audit=events)
    page = watch(toolkit)

    @toolkit.secured_tool(effect="write")
    def post(text: str) -> str:
        return "posted"

    post(text="x")
    assert _events(page)["events"][0]["outcome"] == "ran"
    page.close()
    assert toolkit.audit is events, "the previous sink is given back"
    page.close()  # idempotent
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

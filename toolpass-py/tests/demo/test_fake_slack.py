"""The demo's Slack stand-in answers the real slack integration's permission
checks from world.py, and serves the demo tools' own calls."""

from __future__ import annotations

import json
import sys
import urllib.parse
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

_DEMO = str(Path(__file__).resolve().parents[2] / "examples" / "demo")
if _DEMO not in sys.path:
    sys.path.insert(0, _DEMO)

from fake_slack import SlackFake, connection  # noqa: E402
from world import DANA, SAM  # noqa: E402

from toolpass import Toolpass  # noqa: E402
from toolpass.core.routes import Request  # noqa: E402
from toolpass.integrations.slack.routes import ROUTES  # noqa: E402

CONN = "slack-acme"


@pytest.fixture
def fake() -> Iterator[SlackFake]:
    f = SlackFake()
    f.start()
    yield f
    f.stop()


@pytest.fixture
def settings(fake: SlackFake, tmp_path: Path) -> dict[str, str]:
    return connection(fake.url, tmp_path)


@pytest.fixture
def tp(settings: dict[str, str]) -> Toolpass:
    return Toolpass(connections=[settings])


def route(settings: dict[str, str], method: str, path: str, query: str = "", body: bytes | None = None, ctype: str = "") -> tuple[str | None, str | None]:
    """The action and resource the wire hook would check for a request,
    stripping the connection's path prefix as it does."""
    u = urllib.parse.urlsplit(settings["url"])
    ((host, prefix),) = ROUTES.endpoints({"url": settings["url"]})
    assert host.endswith(f":{u.port}") or host == u.hostname
    req = Request("http", u.hostname or "", u.port, method, prefix + path, query, {"content-type": ctype} if ctype else {}, body)
    obs = ROUTES.match(req, prefix)
    assert obs is not None and obs.unresolved is None, obs
    return obs.action, obs.resource


def call(fake: SlackFake, method: str, data: bytes | None = None, ctype: str = "application/json", query: str = "") -> Any:
    req = urllib.request.Request(
        f"{fake.url}/api/{method}" + (f"?{query}" if query else ""), data=data, headers={"Content-Type": ctype, "Authorization": "Bearer xoxb-tool"}
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        assert resp.status == 200
        return json.loads(resp.read())


@pytest.mark.parametrize(
    ("channel", "ctype", "expect"),
    [
        ("C0SHOPDEV", "application/json", {DANA: "allow", SAM: "allow"}),
        ("C0RELEASE", "application/x-www-form-urlencoded", {DANA: "allow", SAM: "deny"}),
    ],
)
def test_post_message(fake: SlackFake, settings: dict[str, str], tp: Toolpass, channel: str, ctype: str, expect: dict[str, str]) -> None:
    fields = {"channel": channel, "text": "deploying shop-web 1.4.0"}
    body = json.dumps(fields).encode() if "json" in ctype else urllib.parse.urlencode(fields).encode()
    action, resource = route(settings, "POST", "/chat.postMessage", body=body, ctype=ctype)
    assert (action, resource) == ("message.post", f"channel:{channel}")
    for who, outcome in expect.items():
        d = tp.check(who, CONN, action, resource)
        assert d.decision == outcome, (who, d)

    out = call(fake, "chat.postMessage", body, ctype)
    assert out["ok"] is True and out["channel"] == channel
    hist = call(fake, "conversations.history", query=f"channel={channel}")
    assert [m["text"] for m in hist["messages"]] == ["deploying shop-web 1.4.0"]


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_history(fake: SlackFake, settings: dict[str, str], tp: Toolpass, method: str) -> None:
    if method == "GET":
        action, resource = route(settings, "GET", "/conversations.history", query="channel=C0SHOPDEV")
    else:
        action, resource = route(settings, "POST", "/conversations.history", body=b"channel=C0SHOPDEV", ctype="application/x-www-form-urlencoded")
    assert (action, resource) == ("channel.read", "channel:C0SHOPDEV")
    for who in (DANA, SAM):
        d = tp.check(who, CONN, action, resource)
        assert d.decision == "allow", (who, d)

    call(fake, "chat.postMessage", json.dumps({"channel": "C0SHOPDEV", "text": "first"}).encode())
    call(fake, "chat.postMessage", b"channel=C0SHOPDEV&text=second", "application/x-www-form-urlencoded")
    if method == "GET":
        hist = call(fake, "conversations.history", query="channel=C0SHOPDEV")
    else:
        hist = call(fake, "conversations.history", b"channel=C0SHOPDEV", "application/x-www-form-urlencoded")
    assert hist["ok"] is True
    assert [m["text"] for m in hist["messages"]] == ["second", "first"]
    assert call(fake, "conversations.history", query="channel=C0RELEASE")["messages"] == []


def test_unknown_email_is_denied(tp: Toolpass) -> None:
    d = tp.check("nobody@acme.dev", CONN, "channel.read", "channel:C0SHOPDEV")
    assert d.decision == "deny", d


def test_unknown_channel_is_not_ok(fake: SlackFake) -> None:
    assert call(fake, "conversations.history", query="channel=C0NOPE0000") == {"ok": False, "error": "channel_not_found"}


def test_probe(tp: Toolpass) -> None:
    ((cid, ok, detail),) = tp.probe()
    assert (cid, ok) == (CONN, True), detail

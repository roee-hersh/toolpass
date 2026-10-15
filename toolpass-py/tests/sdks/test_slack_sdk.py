"""slack_sdk under a secured tool: its requests (urllib.request) are checked
at the wire. Needs slack_sdk; skipped without it."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from tests import harness
from tests.harness.wire import Engine, requests_seen
from toolpass import ActionRefused, Session, Toolkit

slack_sdk = pytest.importorskip("slack_sdk")

CHANNEL = "C0123456789"


@pytest.fixture
def slack() -> Iterator[harness.Server]:
    with harness.Server(tls=False) as srv:
        srv.json("POST", "/api/chat.postMessage", 200, {"ok": True, "ts": "1.2", "channel": CHANNEL})
        srv.json("POST", "/api/conversations.history", 200, {"ok": True, "messages": [{"text": "hi"}]})
        srv.json("POST", "/api/auth.test", 200, {"ok": True, "user_id": "U1", "team_id": "T1"})
        yield srv


@pytest.fixture
def engine(slack: harness.Server) -> Engine:
    e = Engine("https://github.example.net", slack.url + "/api")
    e.allow[("dana@example.com", "channel.read")] = True
    e.allow[("admin@example.com", "channel.read")] = True
    e.allow[("admin@example.com", "message.post")] = True
    return e


def client(slack: harness.Server) -> Any:
    return slack_sdk.WebClient(token="xoxb-" + harness.CANARY, base_url=slack.url + "/api/")


def test_a_post_the_user_may_not_make_is_refused(slack, engine, events):
    tools = Toolkit(audit=events, permissions=engine)

    @tools.tool()
    def summarize(channel: str) -> str:
        c = client(slack)
        history = c.conversations_history(channel=channel)  # channel.read, allowed
        c.chat_postMessage(channel=channel, text=f"{len(history['messages'])} messages")  # message.post: refused
        return "posted"

    with Session("dana@example.com").active(), pytest.raises(ActionRefused) as e:
        summarize(CHANNEL)
    assert e.value.code == "not_authorized"
    assert requests_seen(slack) == ["POST /api/conversations.history"]
    assert [a["action"] for a in events.last.actions] == ["slack:channel.read", "slack:message.post"]
    assert events.last.actions[1]["resource"] == f"channel:{CHANNEL}"
    assert harness.CANARY not in str(events.last.to_dict())

    with Session("admin@example.com").active():
        assert summarize(CHANNEL) == "posted"


def test_a_channel_named_by_name_is_unresolved(slack, engine, events):
    tools = Toolkit(audit=events, permissions=engine)

    @tools.tool()
    def post() -> str:
        client(slack).chat_postMessage(channel="#general", text="hi")
        return "posted"

    with Session("admin@example.com").active(), pytest.raises(ActionRefused) as e:
        post()
    assert e.value.code == "unresolved_resource"
    assert "C0123456789" in e.value.reason  # the message tells the model what to use instead
    assert requests_seen(slack) == []


def test_the_bots_own_identity_is_free(slack, engine, events):
    tools = Toolkit(audit=events, permissions=engine)

    @tools.tool()
    def whoami() -> str:
        return str(client(slack).auth_test()["user_id"])

    with Session("dana@example.com").active():
        assert whoami() == "U1"
    assert engine.asked == []

"""The slack route table against the Web API description, and its shape."""

from __future__ import annotations

import json

import pytest

from tests.harness.routes import check_routes_against_spec
from toolpass.core.routes import Request
from toolpass.integrations.slack.actions import ACTIONS
from toolpass.integrations.slack.routes import ROUTES

FORM = "application/x-www-form-urlencoded"


def req(method: str, path: str, body: bytes | dict | None = None, ct: str = "application/json", query: str = "") -> Request:
    data = json.dumps(body).encode() if isinstance(body, dict) else body
    return Request("https", "slack.com", 443, method, path, query, {"content-type": ct}, data)


def test_routes_match_the_api_description():
    assert check_routes_against_spec(ROUTES, "slack", host="slack.com") == []


def test_every_route_names_a_known_action():
    for r in ROUTES.routes:
        assert r.action is None or r.action in ACTIONS, r


@pytest.mark.parametrize(
    ("path", "body", "ct", "action", "resource", "effect", "sends_out"),
    [
        ("/api/chat.postMessage", {"channel": "C0123456789", "text": "hi"}, "application/json", "message.post", "channel:C0123456789", "write", True),
        (
            "/api/chat.postMessage",
            {"channel": "C0123456789", "text": "hi", "thread_ts": "1.2"},
            "application/json",
            "message.post_thread",
            "channel:C0123456789",
            "write",
            True,
        ),
        ("/api/chat.postMessage", b"channel=G0123456789&text=hi", FORM, "message.post", "channel:G0123456789", "write", True),
        ("/api/chat.delete", {"channel": "C0123456789", "ts": "1"}, "application/json", "message.post", "channel:C0123456789", "destructive", False),
        ("/api/conversations.history", b"channel=C0123456789", FORM, "channel.read", "channel:C0123456789", "read", False),
        ("/api/conversations.join", b"channel=C0123456789", FORM, "channel.join", "channel:C0123456789", "write", False),
        ("/api/conversations.archive", b"channel=C0123456789", FORM, "channel.archive", "channel:C0123456789", "destructive", False),
        ("/api/conversations.create", b"name=x", FORM, "channel.create", "workspace", "write", False),
        (
            "/api/files.completeUploadExternal",
            {"files": [], "channel_id": "C0123456789"},
            "application/json",
            "file.upload",
            "channel:C0123456789",
            "write",
            True,
        ),
        ("/api/files.upload", b"channels=C0123456789", FORM, "file.upload", "channel:C0123456789", "write", True),
        ("/api/users.info", b"user=U1", FORM, "user.active", "workspace", "read", False),
        ("/api/usergroups.create", b"name=x", FORM, "workspace.admin", "workspace", "write", False),
        ("/api/admin.users.remove", b"", FORM, "org.admin", "workspace", "destructive", False),
        ("/api/admin.users.list", b"", FORM, "org.admin", "workspace", "read", False),
        ("/api/auth.test", b"", FORM, None, "workspace", "read", False),
    ],
)
def test_routing(path, body, ct, action, resource, effect, sends_out):
    o = ROUTES.match(req("POST", path, body, ct))
    assert o is not None
    assert (o.action, o.resource, o.effect, o.sends_out) == (action, resource, effect, sends_out)
    assert o.unresolved is None


def test_a_channel_by_name_or_a_direct_message_is_unresolved():
    for channel in ("#general", "general", "D0123456789", "C01,C02"):
        o = ROUTES.match(req("POST", "/api/chat.postMessage", {"channel": channel, "text": "hi"}))
        assert o is not None and o.unresolved is not None, channel
    o = ROUTES.match(req("POST", "/api/chat.postMessage", {"text": "hi"}))
    assert o is not None and "names no channel" in (o.unresolved or "")


def test_the_channel_may_come_from_the_query():
    o = ROUTES.match(req("GET", "/api/conversations.history", None, "", query="channel=C0123456789&limit=5"))
    assert o is not None and o.resource == "channel:C0123456789"


def test_multipart_upload_fields_are_read():
    boundary = "xyz"
    part = 'Content-Disposition: form-data; name="{}"{}\r\n\r\n{}\r\n'
    body = (
        f"--{boundary}\r\n"
        + part.format("channels", "", "C0123456789")
        + f"--{boundary}\r\n"
        + part.format("file", '; filename="a.txt"', "secret")
        + f"--{boundary}--\r\n"
    ).encode()
    o = ROUTES.match(req("POST", "/api/files.upload", body, f"multipart/form-data; boundary={boundary}"))
    assert o is not None and o.resource == "channel:C0123456789"


def test_app_level_methods_are_left_out_on_purpose():
    assert ROUTES.match(req("POST", "/api/views.open")) is None
    assert ROUTES.is_unrouted("POST", "/views.open") is not None


def test_endpoint_from_the_url_setting():
    s = type("S", (), {"get": lambda self, k: "https://slack.com/api"})()
    assert ROUTES.endpoints(s) == [("slack.com", "/api")]

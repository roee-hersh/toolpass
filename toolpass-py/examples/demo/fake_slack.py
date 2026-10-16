"""A stand-in for the Slack Web API at ``<url>/api/<method>``.

toolpass looks the caller up with users.lookupByEmail, reads the channel with
conversations.info and checks membership with users.conversations; the
answers come from the channels in ``world.py``. Every channel carries a
visible posting rule (everyone in #shop-dev, full members in #releases), so
message.post is decided on membership. The demo's tools post with
chat.postMessage and read back with conversations.history; posted messages
are kept per channel. Like Slack, every answer is HTTP 200 with ``ok``.
"""

from __future__ import annotations

import pathlib
import urllib.parse
from secrets import token_hex
from typing import Any

from fakebase import Fake, Reply, Req
from world import PEOPLE, SLACK_CHANNELS

TEAM_ID = "T0ACME0001"
USER_IDS = {email: "U0" + email.split("@")[0].upper().ljust(8, "0") for email in PEOPLE}  # dana@ -> U0DANA0000

# The posting rule each channel reports in properties.posting_restricted_to.
POSTING = {"shop-dev": {"type": [], "user": []}, "releases": {"type": ["regular"], "user": []}}


class SlackFake(Fake):
    name = "slack"

    def __init__(self) -> None:
        super().__init__()
        self.messages: dict[str, list[dict[str, Any]]] = {}  # channel id -> messages, oldest first
        self.ts = 1760000000

    def handle(self, r: Req) -> Reply:
        method = r.path.removeprefix("/api/")
        args = {**r.query, **_body_fields(r.body)}
        with self.lock:
            return Reply(200, self.method(method, args))

    def method(self, method: str, args: dict[str, str]) -> dict[str, Any]:
        if method == "auth.test":
            return ok(team="Acme", team_id=TEAM_ID, user="toolpass", user_id="U0TOOLPASS", bot_id="B0TOOLPASS")
        if method == "users.lookupByEmail":
            email = args.get("email", "")
            return ok(user=user(email)) if email in USER_IDS else fail("users_not_found")
        if method == "users.conversations":
            email = next((e for e, uid in USER_IDS.items() if uid == args.get("user")), "")
            if not email:
                return fail("user_not_found")
            return ok(channels=[{"id": c["id"], "name": n} for n, c in SLACK_CHANNELS.items() if email in members(n)], response_metadata={"next_cursor": ""})
        if method == "conversations.list":
            return ok(channels=[channel(n) for n, c in SLACK_CHANNELS.items() if not c["private"]], response_metadata={"next_cursor": ""})
        if method == "team.preferences.list":
            return ok(who_can_post_general={"type": [], "user": []})
        if method == "usergroups.list":
            return ok(usergroups=[])

        name = next((n for n, c in SLACK_CHANNELS.items() if c["id"] == args.get("channel")), "")
        if not name:
            return fail("channel_not_found")
        cid = str(SLACK_CHANNELS[name]["id"])
        if method == "conversations.info":
            return ok(channel=channel(name))
        if method == "conversations.members":
            return ok(members=[USER_IDS[e] for e in sorted(members(name))], response_metadata={"next_cursor": ""})
        if method == "chat.postMessage":
            self.ts += 1
            msg = {"type": "message", "user": "U0TOOLPASS", "text": args.get("text", ""), "ts": f"{self.ts}.000100"}
            self.messages.setdefault(cid, []).append(msg)
            return ok(channel=cid, ts=msg["ts"], message=msg)
        if method == "conversations.history":
            return ok(messages=list(reversed(self.messages.get(cid, []))), has_more=False)  # newest first, as Slack lists them
        return fail("unknown_method")


def ok(**fields: Any) -> dict[str, Any]:
    return {"ok": True, **fields}


def fail(code: str) -> dict[str, Any]:
    return {"ok": False, "error": code}


def user(email: str) -> dict[str, Any]:
    """A full member (not an admin, not a guest) of the workspace."""
    return {
        "id": USER_IDS[email],
        "team_id": TEAM_ID,
        "name": email.split("@")[0],
        "real_name": PEOPLE[email],
        "deleted": False,
        "is_admin": False,
        "is_owner": False,
        "is_restricted": False,
        "is_ultra_restricted": False,
        "is_bot": False,
        "profile": {"email": email, "real_name": PEOPLE[email]},
    }


def members(name: str) -> set[str]:
    m = SLACK_CHANNELS[name]["members"]
    assert isinstance(m, set)
    return m


def channel(name: str) -> dict[str, Any]:
    c = SLACK_CHANNELS[name]
    return {
        "id": c["id"],
        "name": name,
        "is_channel": True,
        "is_private": c["private"],
        "is_archived": False,
        "is_general": False,
        "is_member": True,  # the bot is in every channel
        "properties": {"posting_restricted_to": POSTING[name]},
    }


def _body_fields(body: Any) -> dict[str, str]:
    """A JSON object or a form-encoded body as flat strings."""
    if isinstance(body, dict):
        return {k: v if isinstance(v, str) else str(v) for k, v in body.items()}
    if isinstance(body, bytes):
        return dict(urllib.parse.parse_qsl(body.decode("utf-8", "replace"), keep_blank_values=True))
    return {}


def connection(url: str, secrets: pathlib.Path) -> dict[str, str]:
    """The slack-acme connection: a generated bot token in a file."""
    token = secrets / "slack-token"
    token.write_text("xoxb-demo-" + token_hex(16))
    token.chmod(0o600)
    return {"id": "slack-acme", "integration": "slack", "url": url + "/api", "credential": f"file:{token}"}

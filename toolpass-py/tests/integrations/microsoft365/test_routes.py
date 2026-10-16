"""The microsoft365 route table against the Microsoft Graph description, its
refusals, and its resources through the integration's own check."""

from __future__ import annotations

import json
from collections.abc import Iterator

import pytest

from tests import harness as itest
from tests.harness.routes import check_routes_against_spec
from tests.integrations.microsoft365.test_microsoft365 import CHAN_STD, DANA_ID, DRIVE, GROUP_A, TEAM_A, Env, bob, check, dana, perm, to_user
from toolpass.core.catalog import parse_resource
from toolpass.core.decision import Code
from toolpass.core.integration import find_action
from toolpass.core.routes import Request
from toolpass.integrations.microsoft365 import INTEGRATION
from toolpass.integrations.microsoft365.actions import parse_ref
from toolpass.integrations.microsoft365.routes import ROUTES

T = "11111111-2222-3333-4444-555555555555"
G = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
C = "19:abc@thread.tacv2"
D = "b!abc"
JSON = "application/json"


def req(method: str, path: str, body: bytes | dict | None = None, query: str = "", ct: str = JSON, headers: dict | None = None) -> Request:
    data = json.dumps(body).encode() if isinstance(body, dict) else body
    h = {"content-type": ct} if ct else {}
    h.update(headers or {})
    return Request("https", "graph.microsoft.com", 443, method, "/v1.0" + path, query, h, data)


def match(method: str, path: str, body: bytes | dict | None = None, query: str = "", **kw):
    o = ROUTES.match(req(method, path, body, query, **kw))
    assert o is not None, f"{method} {path} has no route"
    return o


def test_routes_match_the_api_description():
    assert check_routes_against_spec(ROUTES, "msgraph", host="graph.microsoft.com") == []


def test_every_route_names_a_known_action():
    extra = {"mail.send_as", "team.member", "user.active"}  # chosen by extractors
    for r in ROUTES.routes:
        assert r.action is None or find_action(INTEGRATION, r.action) is not None, r
    for a in extra:
        assert find_action(INTEGRATION, a) is not None, a


@pytest.mark.parametrize(
    ("method", "path", "body", "query", "action", "resource", "effect", "sends_out"),
    [
        # Files.
        ("GET", f"/drives/{D}/items/01A", None, "", "file.read", f"drive:{D}/item/01A", "read", False),
        ("GET", f"/drives/{D}/items/01A/content", None, "", "file.read", f"drive:{D}/item/01A", "read", False),
        ("GET", f"/drives/{D}/items/01A/children/01B/content", None, "", "file.read", f"drive:{D}/item/01B", "read", False),
        ("GET", f"/drives/{D}/root", None, "", "file.read", f"drive:{D}/item/root", "read", False),
        ("GET", f"/drives/{D}/items/01A/workbook/worksheets", None, "", "file.read", f"drive:{D}/item/01A", "read", False),
        ("GET", f"/drives/{D}/items/01A/workbook/worksheets/S1/range(address='A1:B2')", None, "", "file.read", f"drive:{D}/item/01A", "read", False),
        ("PUT", f"/drives/{D}/items/01A/content", b"data", "", "file.edit", f"drive:{D}/item/01A", "write", False),
        ("PATCH", f"/drives/{D}/items/01A", {"name": "new.txt"}, "", "file.edit", f"drive:{D}/item/01A", "write", False),
        ("POST", f"/drives/{D}/items/01A/children", {"name": "f", "folder": {}}, "", "file.edit", f"drive:{D}/item/01A", "write", False),
        ("POST", f"/drives/{D}/root/children", {"name": "f", "folder": {}}, "", "file.edit", f"drive:{D}/item/root", "write", False),
        ("PUT", f"/drives/{D}/items/01P:/a.txt:/content", b"x", "@microsoft.graph.conflictBehavior=fail", "file.edit", f"drive:{D}/item/01P", "write", False),
        ("PUT", f"/drives/{D}/root:/a.txt:/content", b"x", "@microsoft.graph.conflictBehavior=rename", "file.edit", f"drive:{D}/item/root", "write", False),
        (
            "POST",
            f"/drives/{D}/items/01P:/a.txt:/createUploadSession",
            {"item": {"@microsoft.graph.conflictBehavior": "fail"}},
            "",
            "file.edit",
            f"drive:{D}/item/01P",
            "write",
            False,
        ),
        ("DELETE", f"/drives/{D}/items/01A", None, "", "file.delete", f"drive:{D}/item/01A", "destructive", False),
        ("POST", f"/drives/{D}/items/01A/permanentDelete", None, "", "file.delete", f"drive:{D}/item/01A", "destructive", False),
        ("POST", f"/drives/{D}/items/01A/createLink", {"type": "view"}, "", "file.share", f"drive:{D}/item/01A", "write", True),
        ("POST", f"/drives/{D}/items/01A/invite", {"recipients": []}, "", "file.share", f"drive:{D}/item/01A", "write", True),
        ("DELETE", f"/drives/{D}/items/01A/permissions/p1", None, "", "file.share", f"drive:{D}/item/01A", "destructive", False),
        ("PATCH", f"/drives/{D}/items/01A/workbook/worksheets/S1", {"name": "x"}, "", "file.edit", f"drive:{D}/item/01A", "write", False),
        ("GET", f"/drives/{D}", None, "", None, "", "read", False),
        # Teams and channels.
        ("GET", f"/teams/{T}", None, "", "team.member", f"team:{T}", "read", False),
        ("GET", f"/teams/{T}/members", None, "", "team.member", f"team:{T}", "read", False),
        ("GET", f"/teams/{T}/channels", None, "", "team.member", f"team:{T}", "read", False),
        ("GET", f"/teams/{T}/primaryChannel/messages", None, "", "team.member", f"team:{T}", "read", False),
        ("GET", f"/teams/{T}/channels/{C}", None, "", "channel.read", f"team:{T}/channel/{C}", "read", False),
        ("GET", f"/teams/{T}/channels/{C}/messages", None, "$expand=replies", "channel.read", f"team:{T}/channel/{C}", "read", False),
        ("POST", f"/teams/{T}/channels/{C}/messages", {"body": {"content": "hi"}}, "", "channel.message.post", f"team:{T}/channel/{C}", "write", True),
        ("POST", f"/teams/{T}/channels/{C}/messages/m1/replies", {"body": {}}, "", "channel.message.post", f"team:{T}/channel/{C}", "write", True),
        ("POST", f"/teams/{T}/channels/{C}/messages/m1/softDelete", None, "", "channel.owner", f"team:{T}/channel/{C}", "destructive", False),
        ("PATCH", f"/teams/{T}/channels/{C}", {"displayName": "x"}, "", "channel.owner", f"team:{T}/channel/{C}", "write", False),
        ("POST", f"/teams/{T}/channels/{C}/members", {}, "", "channel.owner", f"team:{T}/channel/{C}", "write", False),
        ("POST", f"/teams/{T}/channels", {"displayName": "x"}, "", "team.owner", f"team:{T}", "write", False),
        ("POST", f"/teams/{T}/members", {}, "", "team.owner", f"team:{T}", "write", False),
        ("DELETE", f"/teams/{T}", None, "", "team.owner", f"team:{T}", "destructive", False),
        # Groups.
        ("GET", f"/groups/{G}", None, "", "group.member", f"group:{G}", "read", False),
        ("GET", f"/groups/{G}/members", None, "", "group.member", f"group:{G}", "read", False),
        ("GET", f"/groups/{G}/team", None, "", "team.member", f"team:{G}", "read", False),
        ("GET", "/groups", None, "", None, "", "read", False),
        # A user's mailbox, calendar and contacts.
        ("POST", "/users/dana@example.com/sendMail", {"message": {"subject": "x"}}, "", "mail.send_as_self", "mailbox:dana@example.com", "write", True),
        (
            "POST",
            "/users/dana@example.com/sendMail",
            {"message": {"from": {"emailAddress": {"address": "DANA@example.com"}}}},
            "",
            "mail.send_as_self",
            "mailbox:dana@example.com",
            "write",
            True,
        ),
        ("POST", "/users/dana@example.com/messages/AAMk1/send", None, "", "mail.send_as_self", "mailbox:dana@example.com", "write", True),
        ("POST", "/users/dana@example.com/messages/AAMk1/reply", {"comment": "ok"}, "", "mail.send_as_self", "mailbox:dana@example.com", "write", True),
        (
            "POST",
            "/users/dana@example.com/mailFolders/inbox/messages/AAMk1/forward",
            {"toRecipients": []},
            "",
            "mail.send_as_self",
            "mailbox:dana@example.com",
            "write",
            True,
        ),
        ("POST", "/users/dana@example.com/messages", {"subject": "draft"}, "", "user.active", "mailbox:dana@example.com", "write", False),
        ("GET", "/users/dana@example.com/messages", None, "", "user.active", "mailbox:dana@example.com", "read", False),
        ("GET", "/users/dana@example.com/messages/AAMk1/attachments", None, "", "user.active", "mailbox:dana@example.com", "read", False),
        ("POST", "/users/dana@example.com/messages/AAMk1/move", {"destinationId": "archive"}, "", "user.active", "mailbox:dana@example.com", "write", False),
        ("DELETE", "/users/dana@example.com/messages/AAMk1", None, "", "user.active", "mailbox:dana@example.com", "destructive", False),
        ("POST", "/users/dana@example.com/mailFolders/inbox/messageRules", {}, "", "user.active", "mailbox:dana@example.com", "write", True),
        ("PATCH", "/users/dana@example.com/mailboxSettings", {}, "", "user.active", "mailbox:dana@example.com", "write", True),
        ("POST", "/users/dana@example.com/events", {"subject": "x"}, "", "user.active", "mailbox:dana@example.com", "write", True),
        ("POST", "/users/dana@example.com/events/E1/accept", {}, "", "user.active", "mailbox:dana@example.com", "write", True),
        ("GET", "/users/dana@example.com/events/E1", None, "", "user.active", "mailbox:dana@example.com", "read", False),
        ("GET", "/users/dana@example.com/calendar/events", None, "", "user.active", "mailbox:dana@example.com", "read", False),
        ("POST", "/users/dana@example.com/contacts", {"givenName": "x"}, "", "user.active", "mailbox:dana@example.com", "write", False),
        ("GET", "/users/dana@example.com/drive", None, "", "user.active", "user:dana@example.com", "read", False),
        ("GET", "/users/dana@example.com/joinedTeams", None, "", "user.active", "user:dana@example.com", "read", False),
        ("GET", "/users/dana@example.com", None, "", None, "", "read", False),
        ("GET", "/users/dana@example.com/manager", None, "", None, "", "read", False),
        # The agent's own account and reads outside the claimed areas.
        ("GET", "/me", None, "", None, "", "read", False),
        ("GET", "/organization", None, "", None, "", "read", False),
    ],
)
def test_routing(method, path, body, query, action, resource, effect, sends_out):
    o = match(method, path, body, query)
    assert o.unresolved is None, o.unresolved
    assert (o.action, o.resource, o.effect, o.sends_out) == (action, resource, effect, sends_out)


def test_a_from_or_sender_naming_another_mailbox_is_send_as():
    for path, body in (
        ("/users/dana@example.com/sendMail", {"message": {"from": {"emailAddress": {"address": "ceo@example.com"}}}}),
        ("/users/dana@example.com/messages", {"sender": {"emailAddress": {"address": "ceo@example.com"}}}),
        ("/users/dana@example.com/messages/AAMk1", {"from": {"emailAddress": {"address": "ceo@example.com"}}}),
        ("/users/dana@example.com/messages/AAMk1/replyAll", {"message": {"from": {"emailAddress": {"address": "ceo@example.com"}}}}),
        ("/users/dana@example.com/messages/AAMk1/createForward", {"message": {"from": {"emailAddress": {"address": "ceo@example.com"}}}}),
        # The catch-all reads the body too.
        ("/users/dana@example.com/mailFolders/a/childFolders/b/childFolders/c/messages", {"from": {"emailAddress": {"address": "ceo@example.com"}}}),
    ):
        method = "PATCH" if path.endswith("AAMk1") else "POST"
        o = match(method, path, body)
        assert (o.action, o.resource, o.unresolved) == ("mail.send_as", "mailbox:ceo@example.com", None), path


@pytest.mark.parametrize(
    ("method", "path", "body", "query", "ct", "why"),
    [
        # Files: listings, moves, copies, paths, a second item, replace.
        ("GET", f"/drives/{D}/items/01A/children", None, "", JSON, "listing"),
        ("GET", f"/drives/{D}/root/delta", None, "", JSON, "listing"),
        ("GET", f"/drives/{D}/root:/a/b.txt:/content", None, "", JSON, "drive-id"),
        ("GET", f"/drives/{D}/items/01A:/b.txt:/content", None, "", JSON, "item"),
        ("GET", f"/drives/{D}/items/01A/listItem/driveItem/content", None, "", JSON, "another item"),
        ("GET", f"/drives/{D}/items/01A", None, "$expand=children", JSON, "$expand"),
        ("GET", f"/drives/{D}/sharedWithMe", None, "", JSON, "drive-id"),
        ("PATCH", f"/drives/{D}/items/01A", {"parentReference": {"id": "02"}}, "", JSON, "moving"),
        ("PATCH", f"/drives/{D}/items/01A", {"name": "a", "Name": "b"}, "", JSON, "more than once"),
        ("PATCH", f"/drives/{D}/items/01A", b"name=x", "", "application/x-www-form-urlencoded", "JSON"),
        ("PATCH", f"/drives/{D}/items/01A", None, "", JSON, "could not be read"),
        ("POST", f"/drives/{D}/items/01A/children", {"name": "a", "@microsoft.graph.conflictBehavior": "replace"}, "", JSON, "replace"),
        ("POST", f"/drives/{D}/items/01A/children", {"name": "a", "file": {}}, "@microsoft.graph.conflictBehavior=replace", JSON, "replace"),
        ("POST", f"/drives/{D}/items/01A/children", {"name": "a", "remoteItem": {"id": "x"}}, "", JSON, "second item"),
        ("PUT", f"/drives/{D}/items/01P:/a.txt:/content", b"x", "", JSON, "conflictBehavior"),
        ("PUT", f"/drives/{D}/items/01P:/a.txt:/content", b"x", "@microsoft.graph.conflictBehavior=replace", JSON, "replace"),
        (
            "PUT",
            f"/drives/{D}/items/01P:/a.txt:/content",
            b"x",
            "@microsoft.graph.conflictBehavior=fail&@Microsoft.Graph.ConflictBehavior=replace",
            JSON,
            "once",
        ),
        ("PUT", f"/drives/{D}/items/01P:/a%2Fb.txt:/content", b"x", "@microsoft.graph.conflictBehavior=fail", JSON, "slash"),
        ("PUT", f"/drives/{D}/items/01P/a.txt:/content", b"x", "@microsoft.graph.conflictBehavior=fail", JSON, "one file name"),
        ("POST", f"/drives/{D}/items/01A/restore", {"parentReference": {"id": "x"}}, "", JSON, "another folder"),
        ("GET", "/drives/bad id/items/01A", None, "", JSON, "drive id"),
        # Teams: ids, other channels, navigation.
        ("GET", "/teams/not-a-guid", None, "", JSON, "GUID"),
        ("GET", f"/teams/{T}/channels/general", None, "", JSON, "channel id"),
        ("GET", f"/teams/{T}/channels/getAllMessages", None, "", JSON, "channel id"),
        ("POST", f"/teams/{T}/channels/general/messages", {}, "", JSON, "channel id"),
        ("GET", f"/teams/{T}/allChannels/{C}/messages", None, "", JSON, "another team"),
        ("GET", f"/teams/{T}/group/drive/items/01A/content", None, "", JSON, "not routed"),
        ("GET", f"/teams/{T}/channels/{C}/filesFolder/children", None, "", JSON, "drive-id"),
        ("GET", f"/teams/{T}", None, "$expand=group", JSON, "$expand"),
        # Groups: files and sites through the group.
        ("GET", f"/groups/{G}/drive/items/01A/content", None, "", JSON, "drive-id"),
        ("GET", f"/groups/{G}/sites/root", None, "", JSON, "drive-id"),
        ("GET", "/groups/not-a-guid/members", None, "", JSON, "GUID"),
        # Mail: another spelling of send, MIME, unreadable bodies, bad users.
        ("POST", "/users/dana@example.com/messages/AAMk1/microsoft.graph.send", None, "", JSON, "documented paths"),
        ("POST", "/users/dana@example.com/messages/AAMk1/Send", None, "", JSON, "documented paths"),
        ("POST", "/users/dana@example.com/mailFolders/a/childFolders/b/childFolders/c/messages/m/send", None, "", JSON, "documented paths"),
        ("PATCH", "/users/dana@example.com/mailFolders/inbox/MessageRules/r1", {}, "", JSON, "documented paths"),
        ("POST", "/users/dana@example.com/sendMail", b"RnJvbTogY2VvQGV4YW1wbGUuY29t", "", "text/plain", "JSON"),
        ("POST", "/users/dana@example.com/sendMail", None, "", JSON, "could not be read"),
        (
            "POST",
            "/users/dana@example.com/sendMail",
            {"message": {"from": {"emailAddress": {"address": "a@example.com"}}, "sender": {"emailAddress": {"address": "b@example.com"}}}},
            "",
            JSON,
            "different mailboxes",
        ),
        ("POST", "/users/dana@example.com/sendMail", {"message": {"from": "ceo@example.com"}}, "", JSON, "recipient object"),
        ("POST", "/users/not an address/sendMail", {"message": {}}, "", JSON, "email address"),
        ("PUT", "/users/dana@example.com/messages/m/$value", b"From: x", "", "text/plain", "MIME"),
        ("PUT", "/users/dana@example.com/mailFolders/a/childFolders/b/childFolders/c/messages/m/$value", b"From: x", "", "text/plain", "MIME"),
        ("GET", "/users/dana@example.com/calendars/C1/events", None, "", JSON, "not routed"),
        ("GET", "/users/dana@example.com/insights/shared", None, "", JSON, "not routed"),
        ("GET", "/users/dana@example.com/drive/root/children", None, "", JSON, "not routed"),
        ("GET", "/users/dana@example.com", None, "$expand=manager", JSON, "$expand"),
        # /me, $batch, other spellings, sites, shares, chats.
        ("GET", "/me/messages", None, "", JSON, "/me"),
        ("GET", "/me/drive/root/children", None, "", JSON, "/me"),
        ("POST", "/$batch", {"requests": [{"id": "1", "method": "GET", "url": "/me"}]}, "", JSON, "$batch"),
        ("GET", "/Drives/x/items/y/content", None, "", JSON, "Drives"),
        ("GET", "/users('dana@example.com')/messages", None, "", JSON, "users("),
        ("GET", "/drives;x=1/b/items/c/content", None, "", JSON, "drives;"),
        ("GET", "/sites/root/lists", None, "", JSON, "SharePoint"),
        ("GET", "/sites", None, "search=x", JSON, "SharePoint"),
        ("GET", "/shares/u!aHR0cHM6Ly9leGFtcGxl/driveItem/content", None, "", JSON, "sharing link"),
        ("GET", "/chats/c1/messages", None, "", JSON, "chats"),
        # Method tunnelling and paths the server may read differently.
        ("POST", f"/drives/{D}/items/01A/checkin", {}, "", JSON, "override"),
        ("GET", f"/drives/{D}/items/01A/content/../../01B/content", None, "", JSON, "dot segment"),
        ("GET", f"/drives/{D}//items/01A/content", None, "", JSON, "empty segment"),
        ("GET", f"/drives/{D}/items/01A//content", None, "", JSON, "empty segment"),
    ],
)
def test_refused(method, path, body, query, ct, why):
    headers = {"x-http-method": "DELETE"} if why == "override" else None
    o = match(method, path, body, query, ct=ct, headers=headers)
    assert o.unresolved is not None, f"{method} {path} was not refused"
    assert why.lower() in o.unresolved.lower(), o.unresolved


def test_beta_and_writes_with_no_route():
    o = ROUTES.match(Request("https", "graph.microsoft.com", 443, "GET", "/beta/drives/x/items/y/content", "", {}, None))
    assert o is not None and o.unresolved is not None
    for method, path in (
        ("POST", "/groups/" + G + "/members/$ref"),
        ("POST", f"/drives/{D}/items/01A/copy"),
        ("POST", "/chats/c1/messages"),
        ("POST", "/me/sendMail"),
        ("POST", "/users/dana@example.com/sendmail"),
        ("POST", "/users/dana@example.com/assignLicense"),
        ("POST", f"/teams/{T}/primaryChannel/messages"),
    ):
        assert ROUTES.match(req(method, path, {})) is None, path
    assert ROUTES.is_unrouted("POST", f"/drives/{D}/items/01A/copy") is not None
    assert ROUTES.is_unrouted("PUT", "/users/dana@example.com/messages/m/$value") is not None


def test_resources_parse_with_the_integrations_parser():
    for method, path, body in (
        ("GET", f"/drives/{D}/items/01A/content", None),
        ("GET", f"/drives/{D}/root", None),
        ("GET", f"/teams/{T}", None),
        ("POST", f"/teams/{T}/channels/{C}/messages", {}),
        ("PATCH", f"/teams/{T}/channels/{C}", {}),
        ("GET", f"/groups/{G}", None),
        ("GET", f"/groups/{G}/team", None),
        ("POST", "/users/dana@example.com/sendMail", {"message": {}}),
        ("POST", "/users/dana@example.com/sendMail", {"message": {"from": {"emailAddress": {"address": "ceo@example.com"}}}}),
        ("GET", "/users/" + G + "/messages", None),
        ("GET", "/users/dana@example.com/drive", None),
    ):
        o = match(method, path, body)
        assert o.action is not None and o.resource is not None, path
        parse_ref(o.action, parse_resource(o.resource))


@pytest.fixture
def env() -> Iterator[Env]:
    e = Env()
    yield e
    e.close()


def test_routed_requests_through_the_real_check(env: Env) -> None:
    _, f, c = env.setup()
    f.perms["direct"] = [perm(["read"], to_user(DANA_ID))]

    def decide(user, method, path, body=None):
        o = match(method, path, body)
        assert o.unresolved is None, o.unresolved
        return check(c, user, o.action, o.resource)

    itest.expect_code(decide(bob, "GET", f"/teams/{TEAM_A}/channels/{CHAN_STD}/messages"), Code.ALLOWED)
    itest.expect_code(decide(dana, "GET", f"/teams/{TEAM_A}/members"), Code.ALLOWED)
    itest.expect_code(decide(bob, "DELETE", f"/teams/{TEAM_A}"), Code.DENIED)
    itest.expect_code(decide(dana, "GET", f"/groups/{GROUP_A}/members"), Code.ALLOWED)
    itest.expect_code(decide(dana, "GET", f"/drives/{DRIVE}/items/direct/content"), Code.ALLOWED)
    itest.expect_code(decide(dana, "PUT", f"/drives/{DRIVE}/items/direct/content", b"new"), Code.DENIED)
    itest.expect_code(decide(dana, "POST", "/users/dana@example.com/sendMail", {"message": {}}), Code.ALLOWED)
    itest.expect_code(decide(dana, "GET", "/users/dana@example.com/messages"), Code.ALLOWED)
    # Another user's mailbox, or a message sent as someone else, is unknown.
    itest.expect_code(decide(bob, "POST", "/users/dana@example.com/sendMail", {"message": {}}), Code.UNSUPPORTED)
    itest.expect_code(decide(bob, "GET", "/users/dana@example.com/messages"), Code.UNSUPPORTED)
    from_ceo = {"message": {"from": {"emailAddress": {"address": "ceo@example.com"}}}}
    itest.expect_code(decide(dana, "POST", "/users/dana@example.com/sendMail", from_ceo), Code.UNSUPPORTED)


def test_endpoint_from_the_url_setting():
    s = type("S", (), {"get": lambda self, k: "https://graph.microsoft.us"})()
    assert ROUTES.endpoints(s) == [("graph.microsoft.us", "")]

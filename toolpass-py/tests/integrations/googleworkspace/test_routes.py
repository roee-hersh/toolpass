"""The googleworkspace route table against the Drive, Gmail, Calendar and
Directory discovery documents, and its shape."""

from __future__ import annotations

import json

import pytest

from tests.harness.routes import check_routes_against_spec
from toolpass.core.catalog import parse_resource
from toolpass.core.integration import Settings, find_action
from toolpass.core.routes import Request
from toolpass.integrations.googleworkspace import GoogleWorkspace
from toolpass.integrations.googleworkspace.actions import parse_ref
from toolpass.integrations.googleworkspace.routes import HOSTS, ROUTES

SPECS = ("google-drive", "google-gmail", "google-calendar", "google-directory")
FILE = "1AbCdEfGhIjKlMnOpQrStUvWxYz"
FOLDER = "0FoLdErIdAbCdEfGhIjKl"
BOUNDARY = "===============7330845974216740156=="


def req(
    method: str,
    path: str,
    query: str = "",
    body: object = None,
    *,
    host: str = "www.googleapis.com",
    ct: str = "application/json",
    headers: dict[str, str] | None = None,
) -> Request:
    raw = body if isinstance(body, bytes) else (json.dumps(body).encode() if body is not None else None)
    h = {"content-type": ct} if raw is not None else {}
    h.update(headers or {})
    return Request("https", host, 443, method, path, query, h, raw)


def multipart(metadata: str, eol: str = "\r\n", boundary: str = BOUNDARY, part_headers: str = "Content-Type: application/json; charset=UTF-8") -> bytes:
    d = "--" + boundary
    return (f"{d}{eol}{part_headers}{eol}MIME-Version: 1.0{eol}{eol}{metadata}{eol}{d}{eol}Content-Type: text/plain{eol}{eol}hello{eol}{d}--{eol}").encode()


def test_routes_match_the_api_descriptions():
    assert check_routes_against_spec(ROUTES, SPECS, host="www.googleapis.com") == []


def test_every_route_names_a_known_action():
    integration = GoogleWorkspace()
    for r in ROUTES.routes:
        assert r.action is None or find_action(integration, r.action) is not None, r


ROUTING = [
    # Drive reads.
    ("GET", f"/drive/v3/files/{FILE}", "", None, "drive.file.read", f"file:{FILE}", "read", False),
    ("GET", f"/drive/v3/files/{FILE}", "alt=media", None, "drive.file.download", f"file:{FILE}", "read", False),
    ("GET", f"/drive/v3/files/{FILE}/export", "mimeType=text/plain", None, "drive.file.download", f"file:{FILE}", "read", False),
    ("POST", f"/drive/v3/files/{FILE}/download", "", None, "drive.file.download", f"file:{FILE}", "read", False),
    ("GET", f"/drive/v3/files/{FILE}/permissions", "", None, "drive.file.read", f"file:{FILE}", "read", False),
    ("GET", f"/drive/v3/files/{FILE}/revisions/42", "alt=media", None, "drive.file.download", f"file:{FILE}", "read", False),
    ("GET", f"/drive/v3/files/{FILE}/accessproposals", "", None, "drive.file.share", f"file:{FILE}", "read", False),
    ("GET", f"/drive/v3/drives/{FOLDER}", "", None, "drive.file.read", f"file:{FOLDER}", "read", False),
    ("GET", "/drive/v3/files", "q=name%3D%27x%27", None, None, "", "read", False),
    ("GET", "/drive/v3/about", "fields=user", None, None, "", "read", False),
    # Drive writes.
    ("POST", "/drive/v3/files", "", {"name": "notes", "parents": [FOLDER]}, "drive.folder.add_child", f"file:{FOLDER}", "write", False),
    ("PATCH", f"/drive/v3/files/{FILE}", "", {"description": "x"}, "drive.file.edit", f"file:{FILE}", "write", False),
    ("PATCH", f"/drive/v3/files/{FILE}", "", {"name": "renamed"}, "drive.file.rename", f"file:{FILE}", "write", False),
    ("PATCH", f"/drive/v3/files/{FILE}", "", {"trashed": True}, "drive.file.trash", f"file:{FILE}", "write", False),
    ("PATCH", f"/drive/v3/files/{FILE}", "", {"writersCanShare": False}, "drive.file.share", f"file:{FILE}", "write", False),
    ("DELETE", f"/drive/v3/files/{FILE}", "", None, "drive.file.delete", f"file:{FILE}", "destructive", False),
    ("POST", f"/drive/v3/files/{FILE}/copy", "", {"name": "copy"}, "drive.file.copy", f"file:{FILE}", "write", False),
    (
        "POST",
        f"/drive/v3/files/{FILE}/permissions",
        "",
        {"role": "reader", "type": "user", "emailAddress": "x@y.com"},
        "drive.file.share",
        f"file:{FILE}",
        "write",
        True,
    ),
    ("PATCH", f"/drive/v3/files/{FILE}/permissions/123", "", {"role": "writer"}, "drive.file.share", f"file:{FILE}", "write", True),
    ("DELETE", f"/drive/v3/files/{FILE}/permissions/123", "", None, "drive.file.share", f"file:{FILE}", "destructive", False),
    ("POST", f"/drive/v3/files/{FILE}/accessproposals/p1:resolve", "", {"action": "ACCEPT"}, "drive.file.share", f"file:{FILE}", "write", True),
    ("POST", f"/drive/v3/files/{FILE}/comments", "", {"content": "hi"}, "drive.file.comment", f"file:{FILE}", "write", True),
    ("POST", f"/drive/v3/files/{FILE}/comments/c1/replies", "", {"content": "hi"}, "drive.file.comment", f"file:{FILE}", "write", True),
    ("PATCH", f"/drive/v3/files/{FILE}/revisions/42", "", {"keepForever": True}, "drive.file.edit", f"file:{FILE}", "write", True),
    ("PATCH", f"/drive/v3/files/{FILE}/revisions/42", "", {"published": True}, "drive.file.share", f"file:{FILE}", "write", True),
    ("POST", f"/drive/v3/files/{FILE}/watch", "", {"id": "c", "type": "web_hook", "address": "https://x"}, "drive.file.read", f"file:{FILE}", "write", True),
    # Gmail.
    ("GET", "/gmail/v1/users/dana@example.com/messages", "q=is%3Aunread", None, "mail.delegate_access", "mailbox:dana@example.com", "read", False),
    ("GET", "/gmail/v1/users/dana%40example.com/messages/m1", "format=full", None, "mail.delegate_access", "mailbox:dana@example.com", "read", False),
    ("POST", "/gmail/v1/users/dana@example.com/messages/send", "", {"raw": "eA"}, "mail.delegate_access", "mailbox:dana@example.com", "write", True),
    ("POST", "/gmail/v1/users/dana@example.com/drafts/send", "", {"id": "d1"}, "mail.delegate_access", "mailbox:dana@example.com", "write", True),
    ("POST", "/gmail/v1/users/dana@example.com/drafts", "", {"message": {"raw": "eA"}}, "mail.delegate_access", "mailbox:dana@example.com", "write", False),
    (
        "POST",
        "/gmail/v1/users/dana@example.com/messages/m1/modify",
        "",
        {"addLabelIds": ["X"]},
        "mail.delegate_access",
        "mailbox:dana@example.com",
        "write",
        False,
    ),
    ("DELETE", "/gmail/v1/users/dana@example.com/threads/t1", "", None, "mail.delegate_access", "mailbox:dana@example.com", "destructive", False),
    (
        "POST",
        "/gmail/v1/users/dana@example.com/messages/batchDelete",
        "",
        {"ids": ["a"]},
        "mail.delegate_access",
        "mailbox:dana@example.com",
        "destructive",
        False,
    ),
    ("GET", "/gmail/v1/users/dana@example.com/settings/filters", "", None, "mail.delegate_access", "mailbox:dana@example.com", "read", False),
    # Calendar.
    (
        "GET",
        "/calendar/v3/calendars/team%40group.calendar.google.com/events",
        "",
        None,
        "calendar.read",
        "calendar:team@group.calendar.google.com",
        "read",
        False,
    ),
    ("GET", "/calendar/v3/calendars/dana@example.com", "", None, "calendar.read", "calendar:dana@example.com", "read", False),
    (
        "POST",
        "/calendar/v3/calendars/dana@example.com/events",
        "sendUpdates=all",
        {"summary": "x"},
        "calendar.event.write",
        "calendar:dana@example.com",
        "write",
        True,
    ),
    ("PATCH", "/calendar/v3/calendars/dana@example.com/events/e1", "", {"summary": "y"}, "calendar.event.write", "calendar:dana@example.com", "write", True),
    ("DELETE", "/calendar/v3/calendars/dana@example.com/events/e1", "", None, "calendar.event.write", "calendar:dana@example.com", "destructive", False),
    ("POST", "/calendar/v3/calendars/dana@example.com/acl", "", {"role": "reader"}, "calendar.share", "calendar:dana@example.com", "write", True),
    ("GET", "/calendar/v3/calendars/dana@example.com/acl", "", None, "calendar.share", "calendar:dana@example.com", "read", False),
    (
        "DELETE",
        "/calendar/v3/calendars/team@group.calendar.google.com",
        "",
        None,
        "calendar.share",
        "calendar:team@group.calendar.google.com",
        "destructive",
        False,
    ),
    ("POST", "/calendar/v3/freeBusy", "", {"items": [{"id": "dana@example.com"}], "timeMin": "x"}, "calendar.read", "calendar:dana@example.com", "read", False),
    ("GET", "/calendar/v3/users/me/calendarList", "", None, None, "", "read", False),
    # Directory.
    ("GET", "/admin/directory/v1/users/dana@example.com", "", None, "user.active", "user:dana@example.com", "read", False),
    ("GET", "/admin/directory/v1/groups/eng@example.com/members", "", None, "group.member", "group:eng@example.com", "read", False),
    # Docs, Sheets, Slides.
    ("GET", f"/v1/documents/{FILE}", "", None, "drive.file.download", f"file:{FILE}", "read", False),
    ("POST", f"/v1/documents/{FILE}:batchUpdate", "", {"requests": []}, "drive.file.edit", f"file:{FILE}", "write", True),
    ("GET", f"/v4/spreadsheets/{FILE}/values/Sheet1!A1:B2", "", None, "drive.file.download", f"file:{FILE}", "read", False),
    ("PUT", f"/v4/spreadsheets/{FILE}/values/Sheet1!A1", "valueInputOption=RAW", {"values": [[1]]}, "drive.file.edit", f"file:{FILE}", "write", True),
    ("POST", f"/v4/spreadsheets/{FILE}/values/Sheet1!A1:append", "valueInputOption=RAW", {"values": [[1]]}, "drive.file.edit", f"file:{FILE}", "write", True),
    ("POST", f"/v4/spreadsheets/{FILE}/values:batchUpdate", "", {"data": []}, "drive.file.edit", f"file:{FILE}", "write", True),
    ("POST", f"/v4/spreadsheets/{FILE}/values:batchGetByDataFilter", "", {"dataFilters": []}, "drive.file.download", f"file:{FILE}", "read", True),
    ("POST", f"/v4/spreadsheets/{FILE}:batchUpdate", "", {"requests": []}, "drive.file.edit", f"file:{FILE}", "write", True),
    ("POST", f"/v1/presentations/{FILE}:batchUpdate", "", {"requests": [{"createSlide": {}}]}, "drive.file.edit", f"file:{FILE}", "write", True),
    # Housekeeping.
    ("GET", "/discovery/v1/apis/drive/v3/rest", "", None, None, "", "read", False),
]


@pytest.mark.parametrize(("method", "path", "query", "body", "action", "resource", "effect", "sends_out"), ROUTING)
def test_routing(method, path, query, body, action, resource, effect, sends_out):
    o = ROUTES.match(req(method, path, query, body))
    assert o is not None and o.unresolved is None, o
    assert (o.action, o.resource, o.effect, o.sends_out) == (action, resource, effect, sends_out)
    if action is not None:
        # The action is one the integration checks, and the resource is in the form its check parses.
        assert find_action(GoogleWorkspace(), action) is not None
        parse_ref(action, parse_resource(resource))


@pytest.mark.parametrize(
    ("method", "path", "query", "body"),
    [
        ("GET", "/gmail/v1/users/me/messages", "", None),  # me is the agent's own mailbox
        ("POST", "/gmail/v1/users/me/messages/send", "", {"raw": "eA"}),
        ("GET", "/calendar/v3/calendars/primary/events", "", None),  # the agent's own calendar
        ("POST", "/calendar/v3/calendars/PRIMARY/events", "", {}),
        ("GET", "/drive/v3/files/appDataFolder", "", None),
        ("GET", "/drive/v3/files/root", "", None),
        ("POST", "/drive/v3/files", "", {"name": "x"}),  # no parents: the agent's own My Drive
        ("POST", "/drive/v3/files", "", {"name": "x", "parents": [FOLDER, FILE]}),
        ("POST", "/drive/v3/files", "", {"name": "x", "parents": ["root"]}),
        ("PATCH", f"/drive/v3/files/{FILE}", f"addParents={FOLDER}", {}),  # a move touches a second folder
        ("PATCH", f"/drive/v3/files/{FILE}", f"removeParents={FOLDER}", {}),
        ("PATCH", f"/drive/v3/files/{FILE}", "", {"trashed": True, "name": "x"}),
        ("PATCH", f"/drive/v3/files/{FILE}", "", {"writersCanShare": True, "description": "x"}),
        ("PATCH", f"/drive/v3/files/{FILE}", "", {"name": "a", "Name": "b"}),  # one field twice
        ("PATCH", f"/drive/v3/files/{FILE}", "", {"writersCanShare": False, "writers_can_share": True}),
        ("POST", f"/drive/v3/files/{FILE}/copy", "", {"parents": [FOLDER]}),
        ("POST", f"/drive/v3/files/{FILE}/permissions", "transferOwnership=true", {"role": "owner"}),
        ("POST", f"/drive/v3/files/{FILE}/permissions", "", {"role": "owner"}),
        ("PATCH", f"/drive/v3/files/{FILE}/permissions/1", "", {"pendingOwner": True}),
        ("POST", f"/drive/v3/files/{FILE}/permissions", "useDomainAdminAccess=true", {"role": "reader"}),
        ("GET", f"/drive/v3/files/{FILE}", "alt=json&alt=media", None),  # the server may read either
        ("PATCH", f"/drive/v3/files/{FILE}/revisions/1", "", {"published": True, "keepForever": True}),
        ("POST", f"/drive/v3/files/{FILE}/accessproposals/p1:approve", "", {}),
        ("DELETE", "/drive/v3/files/trash", "", None),
        ("GET", f"/drive/v2/files/{FILE}", "alt=media", None),  # another Drive version reads files too
        ("GET", "/drive/v3/somethingNew", "", None),
        ("POST", "/calendar/v3/freeBusy", "", {"items": [{"id": "a@example.com"}, {"id": "b@example.com"}]}),
        ("POST", "/calendar/v3/freeBusy", "", {"items": [{"id": "primary"}]}),
        ("GET", "/admin/directory/v1/users", "customer=my_customer", None),  # Directory administration data
        ("GET", "/admin/directory/v1/users/dana@example.com/tokens", "", None),
        ("GET", "/admin/directory/v1/users/123456", "", None),
        ("GET", "/gmail/v1/users/a%3Fb@example.com/messages", "", None),  # parses as a resource query
        ("POST", "/batch/drive/v3", "", b"--x\r\n"),
        ("POST", f"/v1/presentations/{FILE}:batchUpdate", "", {"requests": [{"createSheetsChart": {"spreadsheetId": FOLDER}}]}),
        ("POST", f"/v4/spreadsheets/{FILE}/sheets/0:copyTo", "", {"destinationSpreadsheetId": FOLDER}),
        ("POST", "/v1/documents", "", {"title": "x"}),
        ("GET", f"/v1/documents/{FILE}:batchUpdate", "", None),
    ],
)
def test_requests_toolpass_cannot_read_exactly_are_refused(method, path, query, body):
    o = ROUTES.match(req(method, path, query, body))
    assert o is not None and o.unresolved, o


def test_a_method_override_is_refused():
    o = ROUTES.match(req("POST", f"/drive/v3/files/{FILE}/comments", "", {"content": "x"}, headers={"x-http-method-override": "DELETE"}))
    assert o is not None and o.unresolved and "X-HTTP-Method-Override" in o.unresolved


def test_json_that_parsers_read_differently_is_refused():
    dup = b'{"parents": ["' + FOLDER.encode() + b'"], "parents": ["' + FILE.encode() + b'"]}'
    o = ROUTES.match(req("POST", "/drive/v3/files", "", dup))
    assert o is not None and o.unresolved and "more than once" in o.unresolved
    o = ROUTES.match(req("POST", "/drive/v3/files", "", b'{"parents": NaN}'))
    assert o is not None and o.unresolved
    # A body Drive reads as JSON must say so.
    o = ROUTES.match(req("POST", "/drive/v3/files", "", json.dumps({"parents": [FOLDER]}).encode(), ct="text/plain"))
    assert o is not None and o.unresolved


def test_a_path_that_is_not_utf8_is_refused():
    o = ROUTES.match(req("GET", "/gmail/v1/users/dana@example.com/messages/%FF"))
    assert o is not None and o.unresolved and "UTF-8" in o.unresolved


def test_a_body_the_hook_cannot_see_is_refused():
    o = ROUTES.match(req("PATCH", f"/drive/v3/files/{FILE}", headers={"content-length": "40"}))
    assert o is not None and o.unresolved and "stream" in o.unresolved


@pytest.mark.parametrize("eol", ["\r\n", "\n"])
def test_multipart_upload_reads_the_metadata_part(eol):
    body = multipart(json.dumps({"name": "a.txt", "parents": [FOLDER]}), eol)
    ct = f'multipart/related; boundary="{BOUNDARY}"'
    o = ROUTES.match(req("POST", "/upload/drive/v3/files", "uploadType=multipart", body, ct=ct))
    assert o is not None and o.unresolved is None, o
    assert (o.action, o.resource) == ("drive.folder.add_child", f"file:{FOLDER}")


@pytest.mark.parametrize(
    "body",
    [
        b"preamble\r\n" + multipart(json.dumps({"parents": [FOLDER]})),  # text before the first boundary
        multipart(json.dumps({"parents": [FOLDER]}), part_headers="Content-Type: text/plain"),
        multipart(json.dumps({"parents": [FOLDER]}), part_headers="Content-Type: application/json\r\nContent-Transfer-Encoding: base64"),
        multipart(json.dumps({"parents": [FOLDER]}), part_headers="Content-Type: application/json\r\n folded"),
        multipart(json.dumps({"parents": [FOLDER]}), part_headers="Content-Type: application/json\r\nContent-Type: application/json"),
        multipart('{"parents": ["' + FOLDER + '"], "parents": ["' + FILE + '"]}'),
        ("--" + BOUNDARY + "\r\nContent-Type: application/json\r\n\r\n" + json.dumps({"parents": [FOLDER]}) + "\r\n--" + BOUNDARY + "X\r\n").encode(),
    ],
)
def test_multipart_bodies_read_more_than_one_way_are_refused(body):
    ct = f"multipart/related; boundary={BOUNDARY}"
    o = ROUTES.match(req("POST", "/upload/drive/v3/files", "uploadType=multipart", body, ct=ct))
    assert o is not None and o.unresolved, o


def test_uploads():
    ct = "application/json; charset=UTF-8"
    # A resumable upload is opened with the metadata and continued with its upload_id.
    o = ROUTES.match(req("POST", "/upload/drive/v3/files", "uploadType=resumable", {"parents": [FOLDER]}, ct=ct))
    assert o is not None and (o.action, o.resource, o.unresolved) == ("drive.folder.add_child", f"file:{FOLDER}", None)
    o = ROUTES.match(req("PUT", "/upload/drive/v3/files", "uploadType=resumable&upload_id=abc", b"data", ct="text/plain"))
    assert o is not None and o.action is None and o.unresolved is None
    o = ROUTES.match(req("PUT", "/upload/drive/v3/files", "uploadType=resumable", b"data", ct="text/plain"))
    assert o is not None and o.unresolved
    # Replacing a file's content is an edit; trashing it in the same request is refused.
    o = ROUTES.match(req("PATCH", f"/upload/drive/v3/files/{FILE}", "uploadType=media", b"data", ct="text/plain"))
    assert o is not None and (o.action, o.unresolved) == ("drive.file.edit", None)
    o = ROUTES.match(req("PATCH", f"/upload/drive/v3/files/{FILE}", "uploadType=resumable", {"trashed": True}))
    assert o is not None and o.unresolved
    # A media upload names no folder.
    o = ROUTES.match(req("POST", "/upload/drive/v3/files", "uploadType=media", b"data", ct="text/plain"))
    assert o is not None and o.unresolved
    o = ROUTES.match(req("POST", "/upload/drive/v3/files", "uploadType=other", b"data", ct="text/plain"))
    assert o is not None and o.unresolved
    # Gmail: the message is the upload; sending it sends out.
    o = ROUTES.match(req("POST", "/upload/gmail/v1/users/dana@example.com/messages/send", "uploadType=media", b"From: x", ct="message/rfc822"))
    assert o is not None and (o.action, o.resource, o.sends_out) == ("mail.delegate_access", "mailbox:dana@example.com", True)


def test_unrouted_writes_have_no_route():
    for method, path in [
        ("PUT", "/gmail/v1/users/dana@example.com/settings/autoForwarding"),
        ("POST", "/gmail/v1/users/dana@example.com/settings/delegates"),
        ("POST", "/gmail/v1/users/dana@example.com/settings/sendAs"),
        ("POST", "/admin/directory/v1/groups/eng@example.com/members"),
        ("POST", "/calendar/v3/calendars"),
        ("POST", "/calendar/v3/calendars/dana@example.com/events/e1/move"),
        ("POST", "/drive/v3/drives"),
        ("POST", f"/drive/v3/files/{FILE}/approvals:start"),
    ]:
        assert ROUTES.match(req(method, path, "", {})) is None, path
        assert ROUTES.is_unrouted(method, path), path
    assert ROUTES.match(req("POST", "/youtube/v3/videos", "", {})) is None, "a write to another API on the host is refused at the wire"


def test_a_head_request_is_refused_like_an_unknown_read():
    o = ROUTES.match(req("HEAD", f"/drive/v3/files/{FILE}"))
    assert o is not None and o.unresolved


def test_the_hosts_are_googles_and_the_api_url():
    eps = ROUTES.endpoints(Settings("gws", "googleworkspace", {"api_url": "https://www.googleapis.com"}))
    assert eps == [(h, "") for h in HOSTS]
    eps = ROUTES.endpoints(Settings("gws", "googleworkspace", {"api_url": "https://google-proxy.example:8443/g"}))
    assert ("google-proxy.example:8443", "/g") in eps and ("gmail.googleapis.com", "") in eps

"""Google Workspace API requests (Drive v3, Gmail v1, Calendar v3, Directory
v1), mapped to the googleworkspace integration's actions.

Drive checks the user's capability on the file the request touches
(``file:<id>``): reading metadata is ``drive.file.read``, content
``drive.file.download``, a change ``drive.file.edit`` (``rename``,
``trash`` when that is all it does), sharing ``drive.file.share`` and sends
out, a comment ``drive.file.comment`` and sends out. A new file checks
``drive.folder.add_child`` on the one folder named in ``parents``; a file
without one would land in the agent's own My Drive and is refused. Moving a
file (``addParents``), copying it into another folder and ownership
transfers name a second resource and are refused.

Gmail checks ``mail.delegate_access`` on the mailbox in the path: the
user's own or one they are an accepted delegate of. ``me`` is the agent's
own account, so the mailbox must be named by its address. Sending sends
out. Settings changes (forwarding, filters, delegates, send-as) are not
routed: a delegate may read a mailbox but not change its owner's settings,
and no action says the user owns the mailbox.

Calendar checks the calendar in the path, by id: reading events
``calendar.read``, changing them ``calendar.event.write`` (attendees are
told, so it sends out), sharing and the calendar itself
``calendar.share``. ``primary`` is the agent's own calendar and is refused.

The Directory answers ``user.active`` for the user's own record and
``group.member`` for a group's details; every other Directory read and
every Directory write is administration, which no action covers, and is
refused.

Lists and searches (``files.list``, the agent's calendar list) need no
check: they return what the agent's own credential sees, untrusted. Batch
requests, ``X-HTTP-Method-Override`` and administrator access
(``useDomainAdminAccess``) are refused.

Drive content also comes from ``/download/drive/v3/...`` (the media
download service) and from the web hosts (``docs.google.com`` export
links, ``drive.google.com`` and ``drive.usercontent.google.com`` download
links), which take the same bearer token: the first is checked like
``alt=media``, every other ``/download/`` path and everything on the web
hosts is refused.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Mapping
from typing import Any

from toolpass.core.catalog import ResourceError, parse_resource
from toolpass.core.decision import ToolpassError
from toolpass.core.routes import Extractor, Request, Route, RouteTable, endpoint_of_url
from toolpass.integrations._googleapis import Unreadable, body_object, guarded, param, refusing, upload_metadata
from toolpass.integrations.googleworkspace.actions import CALENDAR_ID_RE, EMAIL_RE, FILE_ID_RE, parse_ref

__all__ = ["HOSTS", "ROUTES"]

# Drive's web hosts: they serve a file's content to the API's bearer token
# (export and download links), so they are claimed, and refused.
_WEB_HOSTS = ("docs.google.com", "drive.google.com", "drive.usercontent.google.com")

# The hosts that serve these APIs, besides the connection's api_url: the
# discovery documents' root URLs and their aliases. Paths are matched whole
# (/drive/v3/..., /gmail/v1/...), so one table serves every host.
HOSTS = (
    "www.googleapis.com",
    "content.googleapis.com",
    "drive.googleapis.com",
    "gmail.googleapis.com",
    "admin.googleapis.com",
    "docs.googleapis.com",
    "sheets.googleapis.com",
    "slides.googleapis.com",
    *_WEB_HOSTS,
)

_D = "/drive/v3"
_F = _D + "/files/{fileId}"
_G = "/gmail/v1/users/{userId}"
_C = "/calendar/v3"
_CAL = _C + "/calendars/{calendarId}"
_A = "/admin/directory/v1"
_UPLOADS = ("/upload", "/resumable/upload")

_FILE = "file:{fileId}"
_MAILBOX = "mailbox:{userId}"
_CALENDAR = "calendar:{calendarId}"

# Drive file fields that change who can do what with the file: changing
# them is sharing, not editing.
_SHARING_FIELDS = frozenset(
    {"writerscanshare", "copyrequireswriterpermission", "viewerscancopycontent", "inheritedpermissionsdisabled", "downloadrestrictions"}
)
_PUBLISH_FIELDS = frozenset({"published", "publishauto", "publishedoutsidedomain"})


# -- resources ------------------------------------------------------------------


def _file(fid: str) -> str:
    if fid.casefold() == "appdatafolder":
        raise Unreadable("appDataFolder is the agent's own application folder, not a file of the user's")
    if not FILE_ID_RE.fullmatch(fid):
        raise Unreadable(f"{fid!r} is not a Drive file id; name the file by its id (root and other aliases are the agent's own folders)")
    return f"file:{fid}"


def _calendar(cid: str) -> str:
    if cid.casefold() == "primary":
        raise Unreadable("primary is the agent's own calendar; name the calendar by its id (a user's primary calendar is their email address)")
    if not CALENDAR_ID_RE.fullmatch(cid):
        raise Unreadable(f"{cid!r} is not a calendar id")
    return f"calendar:{cid}"


def _mailbox(uid: str) -> str:
    if not EMAIL_RE.fullmatch(uid):
        raise Unreadable("name the mailbox by its address: me is the agent's own account, not the user's")
    return f"mailbox:{uid}"


def _email(kind: str) -> Callable[[str], str]:
    def make(v: str) -> str:
        if not EMAIL_RE.fullmatch(v):
            raise Unreadable(f"name the {kind} by its email address, not {v!r}")
        return f"{kind}:{v}"

    return make


def _verified(action: str, resource: str) -> dict[str, str]:
    """The action and resource, once the integration's own parser accepts
    them: a resource it would reject is refused here, not at the check."""
    try:
        parse_ref(action, parse_resource(resource))
    except (ToolpassError, ResourceError) as e:
        raise Unreadable(f"{resource} is not a resource the googleworkspace check reads ({e})") from None
    return {"_action": action, "_resource": resource}


def _canon(meta: Mapping[str, Any]) -> dict[str, Any]:
    """Field names as Google's JSON reader matches them: camelCase and
    snake_case name the same field, so they compare without case or
    underscores, and a field given both ways is refused."""
    out: dict[str, Any] = {}
    for k, v in meta.items():
        c = k.replace("_", "").lower()
        if c in out:
            raise Unreadable(f"the body gives the field {k!r} more than once")
        out[c] = v
    return out


Decide = Callable[[Request, Mapping[str, str]], str | None]


def _on(action: str, var: str, kind: Callable[[str], str], decide: Decide | None = None) -> Extractor:
    """An extractor checking ``action`` on the resource named by the path
    variable ``var``; ``decide`` may name another action from the request."""

    def run(r: Request, v: Mapping[str, str]) -> dict[str, str]:
        resource = kind(v[var])
        chosen = decide(r, v) if decide is not None else None
        return _verified(chosen or action, resource)

    return guarded(run)


# -- Drive ------------------------------------------------------------------------


def _download_if_alt(r: Request, v: Mapping[str, str]) -> str | None:
    """``alt=media`` (or anything but json) returns the content."""
    alt = param(r, "alt")
    return "drive.file.download" if alt is not None and alt != "json" else None


def _metadata(r: Request, upload: bool) -> dict[str, Any]:
    """The metadata of a Drive request: the JSON body, or what the upload
    body carries."""
    return _canon(upload_metadata(r)[1] if upload else body_object(r))


def _create(upload: bool) -> Extractor:
    """files.create: the one folder named in parents."""

    def run(r: Request, v: Mapping[str, str]) -> dict[str, str]:
        parents = _metadata(r, upload).get("parents")
        if not isinstance(parents, list) or len(parents) != 1 or not isinstance(parents[0], str):
            raise Unreadable("name the one folder the file goes into (parents); without one it lands in the agent's own My Drive")
        return _verified("drive.folder.add_child", _file(parents[0]))

    return guarded(run)


def _copy(r: Request, v: Mapping[str, str]) -> str | None:
    """files.copy beside the original; a copy into another folder writes a second resource."""
    parents = _canon(body_object(r)).get("parents")
    if parents not in (None, []):
        raise Unreadable("a copy into another folder (parents) writes to a second resource; copy the file beside the original")
    return None


def _update(upload: bool) -> Decide:
    """files.update: trashing, sharing settings and renaming are their own
    capabilities; anything else is an edit. Moves are refused. An upload
    changes the content too, so it is an edit and nothing else."""

    def decide(r: Request, v: Mapping[str, str]) -> str | None:
        for moving in ("addParents", "removeParents"):
            if param(r, moving):
                raise Unreadable(f"{moving} moves the file between folders, which changes a second resource; not routed")
        keys = set(_metadata(r, upload))
        if "trashed" in keys:
            if keys != {"trashed"} or upload:
                raise Unreadable("trash or restore a file on its own, without other changes")
            return "drive.file.trash"
        if keys & _SHARING_FIELDS:
            if keys - _SHARING_FIELDS or upload:
                raise Unreadable("change a file's sharing settings on their own, without other changes")
            return "drive.file.share"
        if keys == {"name"} and not upload:
            return "drive.file.rename"
        return None

    return decide


def _chunk_of_update(r: Request, v: Mapping[str, str]) -> str | None:
    if param(r, "upload_id") is None:
        raise Unreadable("a PUT to an upload path continues a resumable upload and needs its upload_id")
    return None


def _chunk_of_create(r: Request, v: Mapping[str, str]) -> dict[str, str]:
    if param(r, "upload_id") is None:
        raise Unreadable("a PUT to an upload path continues a resumable upload and needs its upload_id")
    return {}


def _share(r: Request, v: Mapping[str, str]) -> str | None:
    """permissions.create and update: an ownership transfer is refused; it
    needs the user to own the file, which canShare does not say."""
    if (param(r, "transferOwnership") or "false").lower() != "false":
        raise Unreadable("transferring ownership needs the user to own the file, which no action checks; not routed")
    meta = _canon(body_object(r))
    if meta.get("role") == "owner" or meta.get("pendingowner") not in (None, False):
        raise Unreadable("making someone the owner needs the user to own the file, which no action checks; not routed")
    return None


def _revision(r: Request, v: Mapping[str, str]) -> str | None:
    """revisions.update: publishing a revision to the web is sharing."""
    keys = set(_canon(body_object(r)))
    if keys & _PUBLISH_FIELDS:
        if keys - _PUBLISH_FIELDS:
            raise Unreadable("publish a revision on its own, without other changes")
        return "drive.file.share"
    return None


def _resolve_proposal(r: Request, v: Mapping[str, str]) -> str | None:
    proposal = v["proposal"]
    if not proposal.endswith(":resolve") or proposal == ":resolve":
        raise Unreadable("the only write on an access proposal is <proposalId>:resolve")
    return None


def _drive() -> list[Route]:
    rd, dl, ed, sh, cm = "drive.file.read", "drive.file.download", "drive.file.edit", "drive.file.share", "drive.file.comment"
    fid = "fileId"
    out = [
        # Reads.
        Route("GET", _D + "/files", None, "", body=guarded(), description="lists what the agent's own credential sees; results are untrusted"),
        Route("GET", _D + "/files/generateIds", None, "", body=guarded(), untrusted_output=False),
        Route("GET", _D + "/files/generateCseToken", None, "", body=refusing("client-side encryption tokens are not routed")),
        Route("GET", _F, rd, _FILE, body=_on(rd, fid, _file, _download_if_alt)),
        Route("GET", _F + "/export", dl, _FILE, body=_on(dl, fid, _file)),
        Route("POST", _F + "/download", dl, _FILE, effect="read", body=_on(dl, fid, _file)),
        Route("GET", _F + "/revisions/{revisionId}", rd, _FILE, body=_on(rd, fid, _file, _download_if_alt)),
        Route("GET", _F + "/accessproposals", sh, _FILE, body=_on(sh, fid, _file)),
        Route("GET", _F + "/accessproposals/{proposalId}", sh, _FILE, body=_on(sh, fid, _file)),
        Route("GET", _F + "/{rest+}", rd, _FILE, body=_on(rd, fid, _file), spec=False, description="comments, permissions, revisions, labels"),
        Route("GET", _D + "/drives/{driveId}", rd, "file:{driveId}", body=_on(rd, "driveId", _file), description="a shared drive's id is its root folder's"),
        Route("GET", _D + "/teamdrives/{teamDriveId}", rd, "file:{teamDriveId}", body=_on(rd, "teamDriveId", _file)),
        Route("GET", _D + "/operations/{name}", None, "", body=guarded(), description="a download the agent started with files.download, checked then"),
    ]
    for path in ("/about", "/apps", "/apps/{appId}", "/changes", "/changes/startPageToken", "/drives", "/teamdrives"):
        out.append(Route("GET", _D + path, None, "", body=guarded(), description="the agent's own account and what it sees; results are untrusted"))
    out += [
        # Files.
        Route("POST", _D + "/files", "drive.folder.add_child", "file:{parent}", body=_create(False)),
        Route("POST", _F + "/copy", "drive.file.copy", _FILE, body=_on("drive.file.copy", fid, _file, _copy)),
        Route("PATCH", _F, ed, _FILE, body=_on(ed, fid, _file, _update(False))),
        Route("DELETE", _F, "drive.file.delete", _FILE, body=_on("drive.file.delete", fid, _file)),
        Route(
            "DELETE", _D + "/files/trash", None, "", body=refusing("emptying the trash deletes the agent's own trashed files, not one file toolpass can check")
        ),
        Route("POST", _F + "/modifyLabels", ed, _FILE, body=_on(ed, fid, _file)),
        Route("POST", _F + "/watch", rd, _FILE, sends_out=True, body=_on(rd, fid, _file), description="change notifications go to a webhook address"),
        # Sharing.
        Route("POST", _F + "/permissions", sh, _FILE, sends_out=True, body=_on(sh, fid, _file, _share)),
        Route("PATCH", _F + "/permissions/{permissionId}", sh, _FILE, sends_out=True, body=_on(sh, fid, _file, _share)),
        Route("DELETE", _F + "/permissions/{permissionId}", sh, _FILE, body=_on(sh, fid, _file)),
        Route("POST", _F + "/accessproposals/{proposal}", sh, _FILE, sends_out=True, body=_on(sh, fid, _file, _resolve_proposal)),
        # Comments notify the people they mention and the file's owners.
        Route("POST", _F + "/comments", cm, _FILE, sends_out=True, body=_on(cm, fid, _file)),
        Route("PATCH", _F + "/comments/{commentId}", cm, _FILE, sends_out=True, body=_on(cm, fid, _file)),
        Route("DELETE", _F + "/comments/{commentId}", cm, _FILE, body=_on(cm, fid, _file)),
        Route("POST", _F + "/comments/{commentId}/replies", cm, _FILE, sends_out=True, body=_on(cm, fid, _file)),
        Route("PATCH", _F + "/comments/{commentId}/replies/{replyId}", cm, _FILE, sends_out=True, body=_on(cm, fid, _file)),
        Route("DELETE", _F + "/comments/{commentId}/replies/{replyId}", cm, _FILE, body=_on(cm, fid, _file)),
        # Revisions: publishing one to the web sends it out.
        Route("PATCH", _F + "/revisions/{revisionId}", ed, _FILE, sends_out=True, body=_on(ed, fid, _file, _revision)),
        Route("DELETE", _F + "/revisions/{revisionId}", ed, _FILE, body=_on(ed, fid, _file)),
        # Anything else under Drive is refused rather than read unchecked.
        *(Route(m, "/drive/{rest+}", None, "", body=refusing("this Drive read is not routed (Drive v3 is)"), spec=False) for m in ("GET", "HEAD")),
        # The media download service: files.get, files.export and revisions.get return content there.
        Route("GET", "/download" + _F, dl, _FILE, body=_on(dl, fid, _file), spec=False),
        Route("GET", "/download" + _F + "/export", dl, _FILE, body=_on(dl, fid, _file), spec=False),
        Route("GET", "/download" + _F + "/revisions/{revisionId}", dl, _FILE, body=_on(dl, fid, _file), spec=False),
    ]
    # Uploads: the same checks, the metadata read from the upload body.
    for up in _UPLOADS:
        out += [
            Route("POST", up + _D + "/files", "drive.folder.add_child", "file:{parent}", body=_create(True), spec=False),
            Route(
                "PUT",
                up + _D + "/files",
                None,
                "",
                body=guarded(_chunk_of_create),
                spec=False,
                description="continues a resumable upload; its folder was checked when it was opened",
            ),
            Route("PATCH", up + _F, ed, _FILE, body=_on(ed, fid, _file, _update(True)), spec=False),
            Route("PUT", up + _F, ed, _FILE, body=_on(ed, fid, _file, _chunk_of_update), spec=False),
        ]
    return out


# -- Gmail ------------------------------------------------------------------------


def _gmail() -> list[Route]:
    da = "mail.delegate_access"
    mb = _on(da, "userId", _mailbox)
    out = [
        Route("GET", _G + "/{rest+}", da, _MAILBOX, body=mb, spec=False, description="messages, threads, drafts, labels, history, settings"),
        Route("GET", _G + "/profile", da, _MAILBOX, body=mb),
        Route("POST", _G + "/messages/send", da, _MAILBOX, sends_out=True, body=mb),
        Route("POST", _G + "/drafts/send", da, _MAILBOX, sends_out=True, body=mb),
        Route("POST", _G + "/watch", da, _MAILBOX, sends_out=True, body=mb, description="notifications go to a Pub/Sub topic"),
        Route("POST", _G + "/stop", da, _MAILBOX, body=mb),
        Route("DELETE", _G + "/messages/{id}", da, _MAILBOX, body=mb),
        Route("POST", _G + "/messages/batchDelete", da, _MAILBOX, effect="destructive", body=mb),
        Route("DELETE", _G + "/threads/{id}", da, _MAILBOX, body=mb),
        Route("DELETE", _G + "/drafts/{id}", da, _MAILBOX, body=mb),
        Route("DELETE", _G + "/labels/{id}", da, _MAILBOX, body=mb),
        Route("PUT", _G + "/drafts/{id}", da, _MAILBOX, body=mb),
        Route("PUT", _G + "/labels/{id}", da, _MAILBOX, body=mb),
        Route("PATCH", _G + "/labels/{id}", da, _MAILBOX, body=mb),
        *(Route(m, "/gmail/{rest+}", None, "", body=refusing("this Gmail read is not routed"), spec=False) for m in ("GET", "HEAD")),
    ]
    for path in (
        "/messages",
        "/messages/import",
        "/messages/batchModify",
        "/messages/{id}/modify",
        "/messages/{id}/trash",
        "/messages/{id}/untrash",
        "/drafts",
        "/threads/{id}/modify",
        "/threads/{id}/trash",
        "/threads/{id}/untrash",
        "/labels",
    ):
        out.append(Route("POST", _G + path, da, _MAILBOX, body=mb))
    # Uploads carry the message itself; a resumable upload's last PUT is the send.
    for up in _UPLOADS:
        for path, sends in (("/messages/send", True), ("/drafts/send", True), ("/messages", False), ("/messages/import", False), ("/drafts", False)):
            for method in ("POST", "PUT"):
                out.append(Route(method, up + _G + path, da, _MAILBOX, sends_out=sends, body=mb, spec=False))
        out.append(Route("PUT", up + _G + "/drafts/{id}", da, _MAILBOX, body=mb, spec=False))
    return out


# -- Calendar ---------------------------------------------------------------------


def _freebusy(r: Request, v: Mapping[str, str]) -> dict[str, str]:
    """freeBusy: one calendar, by id; several (or a group) are refused."""
    items = body_object(r).get("items")
    if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict) or set(items[0]) != {"id"}:
        raise Unreadable("ask for the free/busy of one calendar per request (items: [{id}])")
    cid = items[0]["id"]
    if not isinstance(cid, str):
        raise Unreadable("the calendar id must be a string")
    return _verified("calendar.read", _calendar(cid))


def _calendar_routes() -> list[Route]:
    rd, ew, sh = "calendar.read", "calendar.event.write", "calendar.share"
    cal = "calendarId"
    return [
        # Reads.
        Route("GET", _CAL, rd, _CALENDAR, body=_on(rd, cal, _calendar)),
        Route("GET", _CAL + "/{rest+}", rd, _CALENDAR, body=_on(rd, cal, _calendar), spec=False, description="events and their instances"),
        Route("GET", _CAL + "/acl", sh, _CALENDAR, body=_on(sh, cal, _calendar)),
        Route("GET", _CAL + "/acl/{ruleId}", sh, _CALENDAR, body=_on(sh, cal, _calendar)),
        Route("GET", _C + "/users/me/calendarList/{calendarId}", rd, _CALENDAR, body=_on(rd, cal, _calendar)),
        Route("POST", _C + "/freeBusy", rd, "calendar:{id}", effect="read", body=guarded(_freebusy)),
        Route("GET", _C + "/users/me/calendarList", None, "", body=guarded(), description="the agent's own calendar list; results are untrusted"),
        Route("GET", _C + "/users/me/settings", None, "", body=guarded(), untrusted_output=False, description="the agent's own settings"),
        Route("GET", _C + "/users/me/settings/{setting}", None, "", body=guarded(), untrusted_output=False, description="the agent's own settings"),
        Route("GET", _C + "/colors", None, "", body=guarded(), untrusted_output=False),
        *(Route(m, "/calendar/{rest+}", None, "", body=refusing("this Calendar read is not routed"), spec=False) for m in ("GET", "HEAD")),
        # The calendar itself: its owner's.
        Route("PUT", _CAL, sh, _CALENDAR, body=_on(sh, cal, _calendar)),
        Route("PATCH", _CAL, sh, _CALENDAR, body=_on(sh, cal, _calendar)),
        Route("DELETE", _CAL, sh, _CALENDAR, body=_on(sh, cal, _calendar)),
        Route("POST", _CAL + "/clear", sh, _CALENDAR, effect="destructive", body=_on(sh, cal, _calendar)),
        Route("POST", _CAL + "/transferOwnership", sh, _CALENDAR, sends_out=True, body=_on(sh, cal, _calendar)),
        # Events: attendees receive them.
        Route("POST", _CAL + "/events", ew, _CALENDAR, sends_out=True, body=_on(ew, cal, _calendar)),
        Route("POST", _CAL + "/events/import", ew, _CALENDAR, sends_out=True, body=_on(ew, cal, _calendar)),
        Route("POST", _CAL + "/events/quickAdd", ew, _CALENDAR, sends_out=True, body=_on(ew, cal, _calendar)),
        Route("PUT", _CAL + "/events/{eventId}", ew, _CALENDAR, sends_out=True, body=_on(ew, cal, _calendar)),
        Route("PATCH", _CAL + "/events/{eventId}", ew, _CALENDAR, sends_out=True, body=_on(ew, cal, _calendar)),
        Route("DELETE", _CAL + "/events/{eventId}", ew, _CALENDAR, body=_on(ew, cal, _calendar)),
        Route("POST", _CAL + "/events/watch", rd, _CALENDAR, sends_out=True, body=_on(rd, cal, _calendar), description="notifications go to a webhook"),
        # Sharing.
        Route("POST", _CAL + "/acl", sh, _CALENDAR, sends_out=True, body=_on(sh, cal, _calendar)),
        Route("PUT", _CAL + "/acl/{ruleId}", sh, _CALENDAR, sends_out=True, body=_on(sh, cal, _calendar)),
        Route("PATCH", _CAL + "/acl/{ruleId}", sh, _CALENDAR, sends_out=True, body=_on(sh, cal, _calendar)),
        Route("DELETE", _CAL + "/acl/{ruleId}", sh, _CALENDAR, body=_on(sh, cal, _calendar)),
        Route("POST", _CAL + "/acl/watch", sh, _CALENDAR, sends_out=True, body=_on(sh, cal, _calendar), description="notifications go to a webhook"),
    ]


# -- Directory --------------------------------------------------------------------


def _directory() -> list[Route]:
    group = _on("group.member", "groupKey", _email("group"))
    return [
        Route("GET", _A + "/users/{userKey}", "user.active", "user:{userKey}", body=_on("user.active", "userKey", _email("user"))),
        Route("GET", _A + "/groups/{groupKey}", "group.member", "group:{groupKey}", body=group),
        Route("GET", _A + "/groups/{groupKey}/{rest+}", "group.member", "group:{groupKey}", body=group, spec=False, description="members and aliases"),
        *(
            Route(
                m, "/admin/{rest+}", None, "", body=refusing("Directory administration data: no googleworkspace action says the user may read it"), spec=False
            )
            for m in ("GET", "HEAD")
        ),
    ]


# -- Docs, Sheets, Slides -----------------------------------------------------------
#
# The editors' APIs read and change a Drive file's content by its id: a read
# is drive.file.download, a change drive.file.edit. A change sends out:
# formulas (IMPORTDATA) and images make Google fetch a URL the content names.

_DOC = "/v1/documents/{documentId}"
_SHEET = "/v4/spreadsheets/{spreadsheetId}"
_DECK = "/v1/presentations/{presentationId}"
# Slides requests that read a second file, a spreadsheet's chart.
_SHEETS_CHARTS = frozenset({"createsheetschart", "replaceallshapeswithsheetschart", "refreshsheetschart"})


def _verb_of(var: str, verbs: Mapping[str, str]) -> Callable[[Request, Mapping[str, str]], dict[str, str]]:
    """``<id>`` or ``<id>:<verb>`` in the path: the action for the verb."""

    def run(r: Request, v: Mapping[str, str]) -> dict[str, str]:
        fid, _, verb = v[var].partition(":")
        if verb not in verbs:
            raise Unreadable(f"the method :{verb or '(none)'} is not routed")
        return _read_if_download(r, _verified(verbs[verb], _file(fid)))

    return run


def _deck_update(r: Request, v: Mapping[str, str]) -> dict[str, str]:
    out = _verb_of("presentationId", {"batchUpdate": "drive.file.edit"})(r, v)
    requests = body_object(r).get("requests")
    if not isinstance(requests, list) or not all(isinstance(q, dict) for q in requests):
        raise Unreadable("a batchUpdate carries a list of requests")
    if any(_SHEETS_CHARTS & set(_canon(q)) for q in requests):
        raise Unreadable("a Sheets chart reads a second file, the spreadsheet; not routed")
    return out


def _editors() -> list[Route]:
    dl, ed = "drive.file.download", "drive.file.edit"
    new = refusing("a new document lands in the agent's own My Drive; create it in a folder with the Drive API, then edit it")
    out = [
        Route("GET", _DOC, dl, "file:{documentId}", body=guarded(_verb_of("documentId", {"": dl})), spec=False),
        Route("POST", _DOC, ed, "file:{documentId}", sends_out=True, body=guarded(_verb_of("documentId", {"batchUpdate": ed})), spec=False),
        Route("POST", "/v1/documents", None, "", body=new, spec=False),
        Route("GET", _SHEET, dl, "file:{spreadsheetId}", body=guarded(_verb_of("spreadsheetId", {"": dl})), spec=False),
        Route("GET", _SHEET + "/{rest+}", dl, "file:{spreadsheetId}", body=_on(dl, "spreadsheetId", _file), spec=False, description="values, metadata"),
        Route(
            "POST",
            _SHEET,
            ed,
            "file:{spreadsheetId}",
            sends_out=True,
            body=guarded(_verb_of("spreadsheetId", {"batchUpdate": ed, "getByDataFilter": dl})),
            spec=False,
        ),
        Route("PUT", _SHEET + "/values/{range}", ed, "file:{spreadsheetId}", sends_out=True, body=_on(ed, "spreadsheetId", _file), spec=False),
        Route("POST", _SHEET + "/values/{range}", ed, "file:{spreadsheetId}", sends_out=True, body=_on(ed, "spreadsheetId", _file, _sheet_range), spec=False),
        Route("POST", _SHEET + "/{op}", ed, "file:{spreadsheetId}", sends_out=True, body=guarded(_sheet_op), spec=False),
        Route("POST", _SHEET + "/sheets/{sheet}", None, "", body=refusing("copying a sheet writes into a second spreadsheet; not routed"), spec=False),
        Route("POST", "/v4/spreadsheets", None, "", body=new, spec=False),
        Route("GET", _DECK, dl, "file:{presentationId}", body=guarded(_verb_of("presentationId", {"": dl})), spec=False),
        Route("GET", _DECK + "/{rest+}", dl, "file:{presentationId}", body=_on(dl, "presentationId", _file), spec=False, description="pages, thumbnails"),
        Route("POST", _DECK, ed, "file:{presentationId}", sends_out=True, body=guarded(_deck_update), spec=False),
        Route("POST", "/v1/presentations", None, "", body=new, spec=False),
    ]
    return out


_SHEET_OPS = {
    "values:batchUpdate": "drive.file.edit",
    "values:batchClear": "drive.file.edit",
    "values:batchUpdateByDataFilter": "drive.file.edit",
    "values:batchClearByDataFilter": "drive.file.edit",
    "values:batchGetByDataFilter": "drive.file.download",
    "developerMetadata:search": "drive.file.download",
}


def _sheet_range(r: Request, v: Mapping[str, str]) -> str | None:
    """POST values/<range>:append or :clear."""
    if v["range"].rpartition(":")[2] not in ("append", "clear"):
        raise Unreadable("the only writes on a range are PUT, :append and :clear")
    return None


def _sheet_op(r: Request, v: Mapping[str, str]) -> dict[str, str]:
    """POST values:batchUpdate and the other operations on the whole spreadsheet."""
    action = _SHEET_OPS.get(v["op"])
    if action is None:
        raise Unreadable(f"{v['op']} is not routed")
    return _read_if_download(r, _verified(action, _file(v["spreadsheetId"])))


def _read_if_download(r: Request, out: dict[str, str]) -> dict[str, str]:
    """A POST that only returns content (getByDataFilter) is a read."""
    if out["_action"] == "drive.file.download":
        out["_effect"] = "read"
    return out


# -- the table --------------------------------------------------------------------


def _other_reads(r: Request, v: Mapping[str, str]) -> dict[str, str]:
    """A read of another Google API on the shared hosts: the agent's own
    credential's view, untrusted, as when no route matches."""
    return {}


def _routes() -> list[Route]:
    batch = refusing("a batch request carries several requests toolpass cannot check one by one; send them separately")
    return [
        *_drive(),
        *_gmail(),
        *_calendar_routes(),
        *_directory(),
        *_editors(),
        # The client's own housekeeping.
        Route("GET", "/discovery/v1/apis/{rest+}", None, "", body=guarded(), untrusted_output=False, spec=False, description="API descriptions"),
        Route("GET", "/$discovery/rest", None, "", body=guarded(), untrusted_output=False, spec=False, description="API descriptions"),
        Route(
            "POST", "/oauth2/v4/token", None, "", body=guarded(), effect="read", untrusted_output=False, spec=False, description="the agent's own token refresh"
        ),
        Route("GET", "/oauth2/{rest+}", None, "", body=guarded(), untrusted_output=False, spec=False, description="the agent's own token and identity"),
        Route("POST", "/batch", None, "", body=batch, spec=False),
        Route("POST", "/batch/{rest+}", None, "", body=batch, spec=False),
        # Content from the media download service of an API the table does not route (Drive v2, Cloud Storage).
        *(Route(m, "/download/{rest+}", None, "", body=refusing("this download is not routed (Drive v3's is)"), spec=False) for m in ("GET", "HEAD")),
        # Other APIs' reads on the shared hosts pass, untrusted; the web hosts refuse them (see _web).
        *(
            Route(m, "/{rest+}", None, "", body=guarded(_other_reads), spec=False, description="another Google API's read: not checked, untrusted")
            for m in ("GET", "HEAD")
        ),
    ]


def _web(extract: Extractor | None) -> Extractor:
    """Refuses every request to the web hosts: their export and download
    links serve a file's content to the bearer token, by paths no route
    reads (``/document/d/<id>/export``, ``/uc?id=<id>``)."""

    def run(r: Request, v: Mapping[str, str]) -> Mapping[str, str] | None:
        if r.host.lower().rstrip(".") in _WEB_HOSTS:
            return {"_unresolved": f"{r.host} serves files to the bearer token by paths toolpass does not read; use the Drive API"}
        return extract(r, v) if extract is not None else {}

    return run


def _locate(settings: Any) -> list[tuple[str, str]]:
    """The fixed Google API hosts, and the connection's api_url when it
    names another endpoint (a proxy, a fake)."""
    out = [(h, "") for h in HOSTS]
    get = getattr(settings, "get", None)
    url = get("api_url") if callable(get) else ""
    ep = endpoint_of_url(url) if url else None
    if ep is not None and ep not in out and (ep[0], "") not in out:
        out.append(ep)
    return out


_SETTINGS_REASON = (
    "changes the mailbox owner's settings (forwarding, filters, delegates, send-as); mail.delegate_access also allows delegates, "
    "and no action says the user owns the mailbox"
)

ROUTES = RouteTable(
    "googleworkspace",
    [dataclasses.replace(r, body=_web(r.body)) for r in _routes()],
    claims=(_D, "/gmail/v1", _C, _A, "/admin/directory_v1"),
    unrouted={
        ("*", _G + "/settings/**"): _SETTINGS_REASON,
        ("POST", _D + "/changes/watch"): "watches every change the agent's own credential sees, not one file",
        ("POST", _D + "/channels/stop"): "stops a notification channel by id: not a user resource toolpass can check",
        ("POST", _D + "/drives"): "creates a shared drive owned by the agent's organization: no file to check",
        ("*", _D + "/drives/{driveId}"): "shared drive administration: the integration checks file capabilities, not shared-drive roles",
        ("POST", _D + "/drives/{driveId}/hide"): "hides a shared drive from the agent's own view",
        ("POST", _D + "/drives/{driveId}/unhide"): "unhides a shared drive in the agent's own view",
        ("POST", _D + "/teamdrives"): "creates a shared drive owned by the agent's organization: no file to check",
        ("*", _D + "/teamdrives/{teamDriveId}"): "shared drive administration: the integration checks file capabilities, not shared-drive roles",
        ("POST", _F + "/approvals/{approval}"): "Drive approvals: no googleworkspace action covers approval workflows",
        ("POST", _F + "/approvals:start"): "Drive approvals: no googleworkspace action covers approval workflows",
        ("POST", _C + "/calendars"): "creates a calendar owned by the agent's account: no calendar of the user's to check",
        ("POST", _CAL + "/events/{eventId}/move"): "moves an event into a second calendar (destination) that one check cannot cover",
        ("*", _C + "/users/me/calendarList"): "the agent's own calendar list",
        ("*", _C + "/users/me/calendarList/{calendarId}"): "the agent's own calendar list",
        ("POST", _C + "/users/me/calendarList/watch"): "the agent's own calendar list",
        ("POST", _C + "/users/me/settings/watch"): "the agent's own settings",
        ("POST", _C + "/channels/stop"): "stops a notification channel by id: not a user resource toolpass can check",
        ("*", _A + "/**"): "Directory administration: no googleworkspace action says the user may administer the Directory",
        ("*", "/admin/directory_v1/**"): "Directory administration: no googleworkspace action says the user may administer the Directory",
    },
    locate=_locate,
)

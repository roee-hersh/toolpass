"""Microsoft Graph requests, mapped to the microsoft365 integration's actions.

The table claims the parts of Graph the integration can check:

- drive items, addressed ``/drives/{drive-id}/items/{item-id}`` (or ``/root``):
  ``file.read``, ``file.edit``, ``file.share`` and ``file.delete`` on
  ``drive:<drive id>/item/<item id>``;
- teams and channels: ``team.member`` and ``team.owner`` on ``team:<guid>``,
  ``channel.read``, ``channel.message.post`` and ``channel.owner`` on
  ``team:<guid>/channel/<channel id>``;
- groups, read only: ``group.member`` on ``group:<guid>``;
- the mailbox, calendar and contacts of the user the path names
  (``/users/{email or id}/...``): ``mail.send_as_self`` to send, else
  ``user.active`` on ``mailbox:<email or id>``, which allows the session's
  user their own mailbox only.

It refuses what it cannot tie to one checked resource: ``/me`` (the token's
own account, not necessarily the session's user), ``$batch``, items addressed
by path or through a site, group, user or share, folder listings (a listing
carries every child's download link, and a child may be narrower than its
folder), moves and copies (a second item), a message whose ``from`` or
``sender`` names another mailbox (routed as ``mail.send_as``, which Graph
cannot answer), MIME bodies (they can name any sender), ``$expand`` (it
reaches related resources the check did not cover), and Graph's other
spellings of the claimed paths (another case, OData key syntax, ``/beta``).
Group writes, SharePoint sites, chats and directory administration have no
action to check them and are left out.
"""

from __future__ import annotations

import json
import re
import urllib.parse
from collections.abc import Callable, Mapping
from typing import Any

from toolpass.core.routes import Extractor, Request, Route, RouteTable
from toolpass.integrations.microsoft365.actions import CHANNEL_ID_RE, DRIVE_ID_RE, EMAIL_RE, GUID_RE

__all__ = ["ROUTES"]

# An extractor that always answers (or raises _Refused).
_Ex = Callable[[Request, Mapping[str, str]], dict[str, str]]

_ITEM = "drive:{drive_id}/item/{item}"
_TEAM = "team:{team}"
_CHANNEL = "team:{team}/channel/{channel}"
_GROUP = "group:{group}"
_MAILBOX = "mailbox:{mailbox}"
_ANY = "{resource}"

# A Teams channel id: 19:<id>@thread.<kind>. Stricter than the resource
# parser's pattern so a function name (getAllMessages) is not taken for one.
_TEAMS_CHANNEL_RE = re.compile(r"19:[A-Za-z0-9._-]+@thread\.[A-Za-z0-9]+")
_CONFLICT_KEY = "@microsoft.graph.conflictbehavior"
_VERSION = "/v1.0"
_MAIL_VERBS = frozenset({"send", "reply", "replyall", "forward", "createreply", "createreplyall", "createforward", "sendmail", "messagerules"})
# The first path segments of the claimed areas, and API versions other than
# the one the table strips.
_CLAIMED = frozenset({"drives", "teams", "groups", "users", "me", "sites", "shares", "chats", "$batch", "beta", "v1.0"})


# -- request checks -----------------------------------------------------------


class _Refused(Exception):
    """Raised by a helper to refuse the request with this reason."""


def _guard(r: Request, expand: tuple[str, ...]) -> str | None:
    """What every route refuses: method tunnelling, dot segments and encoded
    slashes (the server may read the path differently), and $expand except
    the listed values."""
    if "x-http-method" in r.headers or "x-http-method-override" in r.headers:
        return "a method override header is not routed; send the request with its own method"
    low = r.raw_path.lower()
    if "%2f" in low or "%5c" in low or "\\" in r.raw_path:
        return "an encoded slash in the path is not routed"
    if any(s in (".", "..") for s in r.path.split("/")):
        return "a dot segment in the path is not routed"
    seen: set[str] = set()
    for k, v in urllib.parse.parse_qsl(r.query, keep_blank_values=True):
        key = k.strip().lower().lstrip("$")
        if key == "expand":
            if key in seen or v.strip().lower() not in expand:
                return "$expand reaches related resources toolpass does not check; request them on their own"
            seen.add(key)
    return None


def _strict_json(r: Request, *, optional: bool = False) -> dict[str, Any]:
    """The body as one JSON object, read strictly: a JSON content type, no
    duplicate member names (compared without case, as Graph may), no NaN.
    An absent body is {} when ``optional``."""
    if not r.body:
        # None is no body or a stream the hook does not read; a stream that
        # says it is JSON may carry what the check needs.
        if optional and (r.body is not None or not r.headers.get("content-type")):
            return {}
        raise _Refused("the request body could not be read; send it as JSON bytes")
    ct = r.content_type
    if ct != "application/json" and not ct.endswith("+json"):
        raise _Refused("the body must be JSON (application/json)")

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for k, v in items:
            if k.lower() in out:
                raise _Refused(f"the member {k!r} is given more than once")
            out[k.lower()] = v
        return out

    def no_constant(name: str) -> Any:
        raise _Refused(f"{name} is not JSON")

    try:
        doc = json.loads(r.body.decode("utf-8"), object_pairs_hook=pairs, parse_constant=no_constant)
    except (ValueError, UnicodeDecodeError):
        raise _Refused("the body is not JSON") from None
    if not isinstance(doc, dict):
        raise _Refused("the body must be a JSON object")
    return doc


def _member(doc: Mapping[str, Any], name: str) -> Any:
    """A member of a strictly read object (its keys are lowercased)."""
    return doc.get(name.lower())


def _conflict(r: Request, doc: Mapping[str, Any] | None, *, required: bool) -> None:
    """The conflict behaviour of a request that creates an item must not be
    replace: replacing an existing item would edit a file the check (on its
    folder) did not cover."""
    values = [v for k, v in urllib.parse.parse_qsl(r.query, keep_blank_values=True) if k.strip().lower() == _CONFLICT_KEY]
    if doc is not None and _CONFLICT_KEY in doc:
        values.append(doc[_CONFLICT_KEY])
    if not values:
        if required:
            raise _Refused("add @microsoft.graph.conflictBehavior=fail (or rename) so an existing item is not replaced")
        return
    if any(not isinstance(v, str) or v.strip().lower() not in ("fail", "rename") for v in values) or len({str(v).lower() for v in values}) != 1:
        raise _Refused("@microsoft.graph.conflictBehavior must be fail or rename, given once: replace would overwrite an item the check did not cover")


def _route(method: str, path: str, action: str | None, resource: str, body: Extractor | None = None, **kw: Any) -> Route:
    """A route whose extractor runs the shared guard first; ``expand`` lists
    the $expand values it lets through."""
    expand: tuple[str, ...] = kw.pop("expand", ())

    def extract(r: Request, vars_: Mapping[str, str]) -> Mapping[str, str] | None:
        why = _guard(r, expand)
        if why is not None:
            return {"_unresolved": why}
        try:
            return body(r, vars_) if body is not None else {}
        except _Refused as e:
            return {"_unresolved": str(e)}

    return Route(method, path, action, resource, body=extract, **kw)


def _refuse(reason: str) -> Extractor:
    def extract(r: Request, vars_: Mapping[str, str]) -> Mapping[str, str]:
        raise _Refused(reason)

    return extract


# -- identifiers --------------------------------------------------------------


def _drive(vars_: Mapping[str, str]) -> None:
    if not DRIVE_ID_RE.fullmatch(vars_["drive_id"]):
        raise _Refused(f"drive id {vars_['drive_id']!r} is not a drive id")


def _item_id(item: str) -> str:
    if not DRIVE_ID_RE.fullmatch(item):
        raise _Refused(f"item {item!r} is not an item id; address the item as /drives/{{drive-id}}/items/{{item-id}}, not by path")
    return item


def _team(vars_: Mapping[str, str]) -> None:
    if not GUID_RE.fullmatch(vars_["team"]):
        raise _Refused(f"team {vars_['team']!r} must be the team's GUID")


def _channel_id(c: str) -> None:
    if not _TEAMS_CHANNEL_RE.fullmatch(c) or not CHANNEL_ID_RE.fullmatch(c):
        raise _Refused(f"channel {c!r} must be a channel id such as 19:abc@thread.tacv2")


def _user(vars_: Mapping[str, str]) -> str:
    u = vars_["user"]
    if "?" in u or not (EMAIL_RE.fullmatch(u) or GUID_RE.fullmatch(u)):
        raise _Refused(f"user {u!r} must be an email address or an object id")
    return u


# -- drive items --------------------------------------------------------------


def _item(var: str | None = "item_id") -> _Ex:
    """The item named by ``var`` (or the drive's root)."""

    def extract(r: Request, vars_: Mapping[str, str]) -> dict[str, str]:
        _drive(vars_)
        return {"item": _item_id(vars_[var]) if var is not None else "root"}

    return extract


# What a read below one item may ask for; each stays on that item.
_ITEM_READS = frozenset({"content", "thumbnails", "versions", "permissions", "listItem", "workbook", "analytics", "activities"})


def _item_read(r: Request, vars_: Mapping[str, str]) -> dict[str, str]:
    """GET below an item: children/{id} moves to that child; a listing
    (children, delta, search) and navigation to another item are refused."""
    _drive(vars_)
    item = vars_.get("item_id", "root")
    segs = vars_["rest"].split("/")
    while len(segs) >= 2 and segs[0] == "children":
        item, segs = segs[1], segs[2:]
    if segs and segs[0] not in _ITEM_READS:
        raise _Refused(
            f"{segs[0]!r} below a drive item is not routed: a folder listing carries every child's download link, and a child may be narrower than its folder"
        )
    if any(s == "driveItem" or s.endswith(":") for s in segs):
        raise _Refused("a path that leads to another item is not routed; address it as /drives/{drive-id}/items/{item-id}")
    return {"item": _item_id(item)}


def _item_patch(r: Request, vars_: Mapping[str, str]) -> dict[str, str]:
    """PATCH an item: a rename is an edit; a move (parentReference) puts it
    in a folder the check did not cover."""
    out = _item()(r, vars_)
    doc = _strict_json(r)
    if _member(doc, "parentReference") is not None:
        raise _Refused("moving an item is not routed: the destination folder is a second item")
    _conflict(r, doc, required=False)
    return out


def _item_create(r: Request, vars_: Mapping[str, str]) -> dict[str, str]:
    """POST children: a new item in the folder, never one that replaces an
    existing child or links another drive's item."""
    out = _item("item_id" if "item_id" in vars_ else None)(r, vars_)
    doc = _strict_json(r)
    for name in ("parentReference", "remoteItem"):
        if _member(doc, name) is not None:
            raise _Refused(f"{name} names a second item; create the item in the folder the path names")
    _conflict(r, doc, required=False)
    return out


def _item_restore(r: Request, vars_: Mapping[str, str]) -> dict[str, str]:
    out = _item()(r, vars_)
    doc = _strict_json(r, optional=True)
    if _member(doc, "parentReference") is not None:
        raise _Refused("restoring into another folder is not routed: the folder is a second item")
    return out


def _by_path(body_conflict: bool) -> Extractor:
    """``/items/{parent-id}:/{name}:/content`` (or ``/root:/{name}:``): a new
    file in the parent folder, checked as an edit of the folder. The name is
    one segment and the conflict behaviour must keep an existing file."""

    def extract(r: Request, vars_: Mapping[str, str]) -> dict[str, str]:
        _drive(vars_)
        parent, name = vars_.get("item_id", vars_.get("root", "")), vars_["name"]
        if not parent.endswith(":") or not name.endswith(":"):
            raise _Refused("address the folder as /items/{parent-id}:/{file name}:/ with one file name")
        parent, name = parent[:-1], name[:-1]
        if "item_id" in vars_:
            _item_id(parent)
        elif parent != "root":
            raise _Refused("address the folder as /root:/{file name}:/ or /items/{parent-id}:/{file name}:/")
        if name in ("", ".", "..") or any(c in name for c in ":/\\"):
            raise _Refused(f"the file name {name!r} must be one path segment")
        doc = _strict_json(r, optional=True) if body_conflict else None
        item = _member(doc, "item") if doc is not None else None
        if item is not None and not isinstance(item, dict):
            raise _Refused("item must be an object")
        _conflict(r, item, required=True)
        return {"item": parent}

    return extract


def _drive_routes() -> list[Route]:
    rd, ed, sh, dl = "file.read", "file.edit", "file.share", "file.delete"
    out: list[Route] = [
        _route("GET", "/drives", None, "", description="drives the agent's account sees: metadata only, items are checked one by one"),
        _route("GET", "/drives/{drive_id}", None, "", description="drive metadata; its items are checked under /items/{item-id}"),
        _route("GET", "/drives/{drive_id}/{rest+}", None, "", body=_refuse("address files as /drives/{drive-id}/items/{item-id}"), spec=False),
        _route("GET", "/drives/{drive_id}/items/{item_id}", rd, _ITEM, body=_item()),
        _route("GET", "/drives/{drive_id}/items/{item_id}/{rest+}", rd, _ITEM, body=_item_read, spec=False),
        _route("GET", "/drives/{drive_id}/root", rd, _ITEM, body=_item(None)),
        _route("GET", "/drives/{drive_id}/root/{rest+}", rd, _ITEM, body=_item_read, spec=False),
        # A new file by path below a folder (the usual way to upload one).
        _route("PUT", "/drives/{drive_id}/items/{item_id}/{name}/content", ed, _ITEM, body=_by_path(False), spec=False),
        _route("PUT", "/drives/{drive_id}/{root}/{name}/content", ed, _ITEM, body=_by_path(False), spec=False),
        _route("POST", "/drives/{drive_id}/items/{item_id}/{name}/createUploadSession", ed, _ITEM, body=_by_path(True), spec=False),
        _route("POST", "/drives/{drive_id}/{root}/{name}/createUploadSession", ed, _ITEM, body=_by_path(True), spec=False),
    ]
    # The same operations on /items/{item-id} and on /root; the API
    # description lists most only for /items.
    spec_root = {("PUT", "/content"), ("DELETE", "/content")}
    for base, var in (("/drives/{drive_id}/items/{item_id}", "item_id"), ("/drives/{drive_id}/root", None)):
        it = _item(var)
        for method, suffix, action, body, kw in (
            ("PATCH", "", ed, _item_patch, {}),
            ("DELETE", "", dl, it, {}),
            ("POST", "/children", ed, _item_create, {}),
            ("PUT", "/content", ed, it, {}),
            ("DELETE", "/content", dl, it, {}),
            ("POST", "/createUploadSession", ed, it, {}),
            ("POST", "/createLink", sh, it, {"sends_out": True}),
            ("POST", "/invite", sh, it, {"sends_out": True}),
            ("POST", "/permissions", sh, it, {"sends_out": True}),
            ("PATCH", "/permissions/{permission_id}", sh, it, {"sends_out": True}),
            ("DELETE", "/permissions/{permission_id}", sh, it, {}),
            ("POST", "/permissions/{permission_id}/grant", sh, it, {"sends_out": True}),
            ("POST", "/checkin", ed, it, {}),
            ("POST", "/checkout", ed, it, {}),
            ("POST", "/discardCheckout", ed, it, {}),
            ("POST", "/restore", ed, _item_restore, {}),
            ("POST", "/permanentDelete", dl, it, {"effect": "destructive"}),
            ("POST", "/follow", rd, it, {}),
            ("POST", "/unfollow", rd, it, {}),
            ("POST", "/preview", rd, it, {"description": "an embeddable preview of the item"}),
            ("POST", "/extractSensitivityLabels", rd, it, {"effect": "read"}),
            ("POST", "/validatePermission", rd, it, {"effect": "read"}),
            ("POST", "/assignSensitivityLabel", ed, it, {}),
            ("PATCH", "/retentionLabel", ed, it, {}),
            ("DELETE", "/retentionLabel", ed, it, {}),
            ("POST", "/versions/{version_id}/restoreVersion", ed, it, {}),
            ("DELETE", "/versions/{version_id}", dl, it, {}),
            ("POST", "/thumbnails", ed, it, {}),
            ("PATCH", "/thumbnails/{thumbnail_id}", ed, it, {}),
            ("DELETE", "/thumbnails/{thumbnail_id}", ed, it, {}),
            ("PUT", "/children/{child_id}/content", ed, _item("child_id"), {}),
            ("DELETE", "/children/{child_id}/content", dl, _item("child_id"), {}),
        ):
            spec = var is not None or (method, suffix) in spec_root
            out.append(_route(method, base + suffix, action, _ITEM, body=body, spec=spec, **kw))
        # Excel workbooks and the SharePoint list item behind a file stay on
        # the file: reads are covered by the GET above.
        for method in ("PATCH", "DELETE"):
            out.append(_route(method, base + "/workbook", ed, _ITEM, body=it, spec=var is not None))
        for method in ("POST", "PATCH", "PUT", "DELETE"):
            out.append(_route(method, base + "/workbook/{rest+}", ed, _ITEM, body=it, spec=False))
            out.append(_route(method, base + "/listItem/{rest+}", ed, _ITEM, body=it, spec=False))
    return out


# -- teams --------------------------------------------------------------------


def _team_only(r: Request, vars_: Mapping[str, str]) -> dict[str, str]:
    _team(vars_)
    return {}


def _channel(r: Request, vars_: Mapping[str, str]) -> dict[str, str]:
    _team(vars_)
    _channel_id(vars_["channel"])
    return {}


# Reads below a team that the team's members may make, and the ones that
# must stop at the collection (a channel's own reads need channel.read).
_TEAM_READS = frozenset({"members", "installedApps", "tags", "photo", "operations"})
_TEAM_LISTS = frozenset({"channels", "allChannels", "incomingChannels", "group", "primaryChannel"})
_CHANNEL_READS = frozenset({"messages", "members", "allMembers", "tabs", "sharedWithTeams"})


def _team_read(r: Request, vars_: Mapping[str, str]) -> dict[str, str]:
    _team(vars_)
    segs = vars_["rest"].split("/")
    if segs[0] in _TEAM_READS or (segs[0] in _TEAM_LISTS and len(segs) == 1):
        return {}
    raise _Refused(f"{'/'.join(segs)!r} below a team is not routed; read a channel as /teams/{{team-id}}/channels/{{channel-id}}/...")


def _channel_read(r: Request, vars_: Mapping[str, str]) -> dict[str, str]:
    _team(vars_)
    if "channel" in vars_:
        _channel_id(vars_["channel"])
    segs = vars_["rest"].split("/")
    if segs[0] in _CHANNEL_READS or (segs[0] == "filesFolder" and len(segs) == 1):
        return {}
    raise _Refused(f"{'/'.join(segs)!r} below a channel is not routed; address its files as /drives/{{drive-id}}/items/{{item-id}}")


def _team_routes() -> list[Route]:
    ow, mb = "team.owner", "team.member"
    rd, post, cow = "channel.read", "channel.message.post", "channel.owner"
    t = "/teams/{team}"
    c = t + "/channels/{channel}"
    out: list[Route] = [
        _route("GET", "/teams", None, "", description="the tenant's teams, as the directory lists them to every member"),
        _route("GET", t, mb, _TEAM, body=_team_only),
        _route("GET", t + "/{rest+}", mb, _TEAM, body=_team_read, spec=False),
        _route("GET", t + "/primaryChannel/{rest+}", mb, _TEAM, body=_channel_read, expand=("replies",), spec=False, description="the General channel"),
        _route("GET", c, rd, _CHANNEL, body=_channel),
        _route("GET", c + "/{rest+}", rd, _CHANNEL, body=_channel_read, expand=("replies",), spec=False),
        _route("GET", t + "/allChannels/{rest+}", mb, _TEAM, body=_refuse("a shared channel may belong to another team; read it there"), spec=False),
        _route("GET", t + "/incomingChannels/{rest+}", mb, _TEAM, body=_refuse("a shared channel belongs to another team; read it there"), spec=False),
    ]
    for method, suffix in (
        ("PATCH", ""),
        ("DELETE", ""),
        ("POST", "/archive"),
        ("POST", "/unarchive"),
        ("POST", "/channels"),
        ("POST", "/members"),
        ("POST", "/members/add"),
        ("POST", "/members/remove"),
        ("PATCH", "/members/{member}"),
        ("DELETE", "/members/{member}"),
        ("POST", "/installedApps"),
        ("PATCH", "/installedApps/{app}"),
        ("DELETE", "/installedApps/{app}"),
        ("POST", "/installedApps/{app}/upgrade"),
        ("POST", "/tags"),
        ("PATCH", "/tags/{tag}"),
        ("DELETE", "/tags/{tag}"),
        ("POST", "/tags/{tag}/members"),
        ("PATCH", "/tags/{tag}/members/{member}"),
        ("DELETE", "/tags/{tag}/members/{member}"),
        ("PATCH", "/photo"),
        ("PUT", "/photo/$value"),
        ("DELETE", "/photo/$value"),
    ):
        out.append(_route(method, t + suffix, ow, _TEAM, body=_team_only))
    # Messages: posting and reacting are a member's; editing and deleting
    # need the channel's owner, since the message may be someone else's.
    for m in (c + "/messages/{message}", c + "/messages/{message}/replies/{reply}"):
        out += [
            _route("PATCH", m, cow, _CHANNEL, body=_channel),
            _route("DELETE", m, cow, _CHANNEL, body=_channel),
            _route("POST", m + "/softDelete", cow, _CHANNEL, body=_channel, effect="destructive"),
            _route("POST", m + "/undoSoftDelete", cow, _CHANNEL, body=_channel),
            _route("POST", m + "/setReaction", post, _CHANNEL, body=_channel),
            _route("POST", m + "/unsetReaction", post, _CHANNEL, body=_channel),
        ]
    for path in (c + "/messages", c + "/messages/replyWithQuote", c + "/messages/{message}/replies", c + "/messages/{message}/replies/replyWithQuote"):
        out.append(_route("POST", path, post, _CHANNEL, body=_channel, sends_out=True))
    for method, suffix, kw in (
        ("PATCH", "", {}),
        ("DELETE", "", {}),
        ("POST", "/archive", {}),
        ("POST", "/unarchive", {}),
        ("POST", "/members", {}),
        ("POST", "/members/add", {}),
        ("POST", "/members/remove", {}),
        ("PATCH", "/members/{member}", {}),
        ("DELETE", "/members/{member}", {}),
        ("POST", "/allMembers", {}),
        ("POST", "/allMembers/add", {}),
        ("POST", "/allMembers/remove", {}),
        ("PATCH", "/allMembers/{member}", {}),
        ("DELETE", "/allMembers/{member}", {}),
        ("POST", "/tabs", {}),
        ("PATCH", "/tabs/{tab}", {}),
        ("DELETE", "/tabs/{tab}", {}),
        ("POST", "/sharedWithTeams", {"sends_out": True}),
        ("PATCH", "/sharedWithTeams/{shared}", {}),
        ("DELETE", "/sharedWithTeams/{shared}", {}),
        ("POST", "/provisionEmail", {"sends_out": True}),
        ("POST", "/removeEmail", {}),
    ):
        out.append(_route(method, c + suffix, cow, _CHANNEL, body=_channel, **kw))
    return out


# -- groups -------------------------------------------------------------------

_GROUP_READS = frozenset(
    {
        "members",
        "transitiveMembers",
        "owners",
        "memberOf",
        "transitiveMemberOf",
        "conversations",
        "threads",
        "events",
        "calendar",
        "calendarView",
        "photo",
        "photos",
        "planner",
        "onenote",
        "acceptedSenders",
        "rejectedSenders",
        "settings",
    }
)


def _group(r: Request, vars_: Mapping[str, str]) -> dict[str, str]:
    g = vars_["group"]
    if not GUID_RE.fullmatch(g):
        raise _Refused(f"group {g!r} must be the group's GUID")
    segs = vars_.get("rest", "").split("/") if vars_.get("rest") else []
    if not segs or segs[0] in _GROUP_READS or (segs[0] in ("drive", "drives") and len(segs) == 1):
        return {"resource": f"group:{g}"}
    if segs == ["team"]:
        return {"resource": f"team:{g}", "_action": "team.member"}
    raise _Refused(f"{'/'.join(segs)!r} below a group is not routed; address files as /drives/{{drive-id}}/items/{{item-id}} and a team as /teams/{{team-id}}")


def _group_routes() -> list[Route]:
    return [
        _route("GET", "/groups", None, "", description="the directory's groups, as every member may list them"),
        _route("GET", "/groups/{group}", "group.member", _ANY, body=_group),
        _route("GET", "/groups/{group}/{rest+}", "group.member", _ANY, body=_group, spec=False),
    ]


# -- users: mailbox, calendar, contacts ---------------------------------------


def _address(v: Any, what: str) -> str:
    """The address of a recipient object {"emailAddress": {"address": ...}}."""
    if not isinstance(v, dict):
        raise _Refused(f"{what} must be a recipient object")
    ea = _member(v, "emailAddress")
    addr = _member(ea, "address") if isinstance(ea, dict) else None
    if not isinstance(addr, str) or not EMAIL_RE.fullmatch(addr):
        raise _Refused(f"{what} must carry an email address")
    return addr


def _sender(field: str | None, *, optional: bool = False) -> _Ex:
    """The mailbox a message is sent from: the path's user, or the address in
    the message's from or sender when it names another mailbox, which is
    then checked as mail.send_as on that mailbox."""

    def extract(r: Request, vars_: Mapping[str, str]) -> dict[str, str]:
        user = _user(vars_)
        doc = _strict_json(r, optional=optional)
        msg: Any = doc if field is None else _member(doc, field)
        if msg is None:
            msg = {}
        if not isinstance(msg, dict):
            raise _Refused(f"{field} must be a message object")
        others = set()
        for name in ("from", "sender"):
            v = _member(msg, name)
            if v is not None:
                addr = _address(v, name)
                if addr.lower() != user.lower():
                    others.add(addr.lower())
        if len(others) > 1:
            raise _Refused("from and sender name different mailboxes")
        if others:
            return {"mailbox": others.pop(), "_action": "mail.send_as"}
        return {"mailbox": user}

    return extract


def _mailbox(r: Request, vars_: Mapping[str, str]) -> dict[str, str]:
    return {"mailbox": _user(vars_)}


def _mail_any(r: Request, vars_: Mapping[str, str]) -> dict[str, str]:
    """Any other request on the mailbox's messages and folders. Sending and
    mail rules have their own routes; their other spellings (another case,
    microsoft.graph.send) and MIME content are refused here."""
    user = _user(vars_)
    if r.method.upper() in ("GET", "HEAD"):
        return {"mailbox": user}
    segs = [s.lower().removeprefix("microsoft.graph.") for s in vars_["rest"].split("/")]
    if any(s in _MAIL_VERBS for s in segs):
        raise _Refused("write sending and mail rules as their documented paths (/send, /reply, /messageRules, ...)")
    if r.method.upper() == "PUT" and segs[-1] == "$value":
        raise _Refused("MIME content is not routed: it can name any sender")
    if r.method.upper() in ("POST", "PATCH"):
        return _sender(None, optional=True)(r, vars_)
    return {"mailbox": user}


_USER_NONE = (("photo",), ("photo", "$value"), ("manager",), ("directReports",))


def _user_read(r: Request, vars_: Mapping[str, str]) -> dict[str, str]:
    """Other reads below a user: the drive and team lists are the user's own;
    the photo and the reporting line are directory data."""
    user = _user(vars_)
    segs = tuple(vars_["rest"].split("/"))
    if segs in (("drive",), ("drives",), ("joinedTeams",)):
        return {"resource": f"user:{user}"}
    if segs in _USER_NONE:
        return {"resource": "", "_action": ""}
    raise _Refused(
        f"{'/'.join(segs)!r} below a user is not routed; the mailbox, calendar and contacts are, and files as /drives/{{drive-id}}/items/{{item-id}}"
    )


def _user_routes() -> list[Route]:
    act, send, own = "user.active", "mail.send_as_self", "user.active"
    u = "/users/{user}"
    out: list[Route] = [
        _route("GET", "/users", None, "", description="the directory's users, as every member may list them"),
        _route("GET", u, None, "", description="a directory profile, readable by every member"),
        _route("GET", u + "/{rest+}", act, _ANY, body=_user_read, spec=False),
        _route("POST", u + "/sendMail", send, _MAILBOX, body=_sender("message"), sends_out=True),
        _route("GET", u + "/messages", own, _MAILBOX, body=_mailbox),
        _route("GET", u + "/mailFolders", own, _MAILBOX, body=_mailbox),
        _route("POST", u + "/mailFolders", own, _MAILBOX, body=_mailbox),
        _route("*", u + "/messages/{rest+}", own, _MAILBOX, body=_mail_any, spec=False),
        _route("*", u + "/mailFolders/{rest+}", own, _MAILBOX, body=_mail_any, spec=False),
        _route("GET", u + "/mailboxSettings", own, _MAILBOX, body=_mailbox),
        _route("GET", u + "/mailboxSettings/{rest+}", own, _MAILBOX, body=_mailbox, spec=False),
        _route("PATCH", u + "/mailboxSettings", own, _MAILBOX, body=_mailbox, sends_out=True, description="automatic replies send mail"),
        _route("GET", u + "/inferenceClassification", own, _MAILBOX, body=_mailbox),
        _route("PATCH", u + "/inferenceClassification", own, _MAILBOX, body=_mailbox),
        _route("*", u + "/inferenceClassification/{rest+}", own, _MAILBOX, body=_mailbox, spec=False),
        _route("GET", u + "/outlook", own, _MAILBOX, body=_mailbox),
        _route("*", u + "/outlook/{rest+}", own, _MAILBOX, body=_mailbox, spec=False),
        _route("GET", u + "/calendars", own, _MAILBOX, body=_mailbox),
        _route("GET", u + "/calendarView", own, _MAILBOX, body=_mailbox),
        _route("GET", u + "/calendarView/{rest+}", own, _MAILBOX, body=_mailbox, spec=False),
    ]
    for area in ("contacts", "contactFolders"):
        out += [
            _route("GET", f"{u}/{area}", own, _MAILBOX, body=_mailbox),
            _route("POST", f"{u}/{area}", own, _MAILBOX, body=_mailbox),
            _route("*", f"{u}/{area}/{{rest+}}", own, _MAILBOX, body=_mailbox, spec=False),
        ]
    # The calendar: every change may notify attendees. Reads come first so
    # they win over the catch-all of the same shape.
    out += [
        _route("GET", u + "/events", own, _MAILBOX, body=_mailbox),
        _route("POST", u + "/events", own, _MAILBOX, body=_mailbox, sends_out=True),
        _route("GET", u + "/events/{rest+}", own, _MAILBOX, body=_mailbox, spec=False),
        _route("*", u + "/events/{rest+}", own, _MAILBOX, body=_mailbox, sends_out=True, spec=False),
        _route("GET", u + "/calendar", own, _MAILBOX, body=_mailbox),
        _route("PATCH", u + "/calendar", own, _MAILBOX, body=_mailbox),
        _route("GET", u + "/calendar/{rest+}", own, _MAILBOX, body=_mailbox, spec=False),
        _route("*", u + "/calendar/{rest+}", own, _MAILBOX, body=_mailbox, sends_out=True, spec=False),
    ]
    # Messages, in the mailbox and in its folders.
    rules = (u + "/mailFolders/{folder}", u + "/mailFolders/{folder}/childFolders/{child}")
    for base in (u + "/messages", *(f + "/messages" for f in rules)):
        out += [
            _route("POST", base, own, _MAILBOX, body=_sender(None)),
            _route("PATCH", base + "/{message}", own, _MAILBOX, body=_sender(None)),
            _route("POST", base + "/{message}/send", send, _MAILBOX, body=_mailbox, sends_out=True, description="sends the draft as it was saved"),
        ]
        for verb in ("reply", "replyAll", "forward"):
            out.append(_route("POST", f"{base}/{{message}}/{verb}", send, _MAILBOX, body=_sender("message"), sends_out=True))
        for verb in ("createReply", "createReplyAll", "createForward"):
            out.append(_route("POST", f"{base}/{{message}}/{verb}", own, _MAILBOX, body=_sender("message", optional=True)))
    # Inbox rules can forward mail anywhere.
    for f in rules:
        out += [
            _route("POST", f + "/messageRules", own, _MAILBOX, body=_mailbox, sends_out=True),
            _route("PATCH", f + "/messageRules/{rule}", own, _MAILBOX, body=_mailbox, sends_out=True),
            _route("DELETE", f + "/messageRules/{rule}", own, _MAILBOX, body=_mailbox),
        ]
    return out


# -- the rest -----------------------------------------------------------------


def _top_level(r: Request, vars_: Mapping[str, str]) -> dict[str, str]:
    """A read outside the claimed areas is not checked. Another spelling of
    a claimed area (Drives, users('x'), /beta) is refused, and so is an empty
    segment, which no other template matches and the server may collapse."""
    raw = r.raw_path[len(_VERSION) :] if r.raw_path.startswith(_VERSION + "/") else r.raw_path
    segs = raw.strip("/").split("/")
    if "" in segs:
        raise _Refused("an empty segment in the path is not routed")
    first = urllib.parse.unquote(segs[0])
    token = re.split(r"[^a-z0-9.$]", first.strip().lower(), maxsplit=1)[0]
    if token in _CLAIMED:
        raise _Refused(f"{first!r} is not routed; write the path as Microsoft Graph v1.0 documents it")
    return {}


def _routes() -> list[Route]:
    me = "/me is the token's own account, which need not be the session's user; name the user: /users/{email}/..."
    out: list[Route] = [
        _route("GET", "/**", None, "", body=_top_level, spec=False, description="reads outside the claimed areas: not checked"),
        _route("GET", "/me", None, "", untrusted_output=False, description="the agent's own account"),
        _route("GET", "/me/{rest+}", None, "", body=_refuse(me), spec=False),
        _route("POST", "/$batch", None, "", body=_refuse("$batch is not routed: send each request on its own so each is checked"), spec=False),
        _route("GET", "/sites", None, "", body=_refuse("SharePoint sites have no check; address files as /drives/{drive-id}/items/{item-id}")),
        _route(
            "GET", "/sites/{rest+}", None, "", body=_refuse("SharePoint sites have no check; address files as /drives/{drive-id}/items/{item-id}"), spec=False
        ),
        _route("GET", "/sites/{site}/drive", None, "", description="the site's default library: metadata, its items are checked by id"),
        _route("GET", "/sites/{site}/drives", None, "", description="the site's libraries: metadata, their items are checked by id"),
        _route(
            "GET",
            "/shares/{rest+}",
            None,
            "",
            body=_refuse("an item by sharing link is not routed; address it as /drives/{drive-id}/items/{item-id}"),
            spec=False,
        ),
        _route("GET", "/chats", None, "", body=_refuse("chats have no check in the microsoft365 integration")),
        _route("GET", "/chats/{rest+}", None, "", body=_refuse("chats have no check in the microsoft365 integration"), spec=False),
    ]
    return out + _drive_routes() + _team_routes() + _group_routes() + _user_routes()


_NO_ACTION = "no microsoft365 action can check it"

ROUTES = RouteTable(
    "microsoft365",
    _routes(),
    claims=("/drives", "/teams", "/groups", "/users", "/me", "/sites", "/shares", "/chats"),
    unrouted={
        ("*", "/me"): "/me is the token's own account, not necessarily the session's user",
        ("*", "/me/**"): "/me is the token's own account, not necessarily the session's user; name the user in /users/{id}",
        ("*", "/sites/**"): f"SharePoint sites, lists and pages: {_NO_ACTION}",
        ("POST", "/shares"): "an item by sharing link: address it by drive and item id",
        ("*", "/shares/**"): "an item by sharing link: address it by drive and item id",
        ("*", "/chats/**"): f"chats: {_NO_ACTION}",
        ("POST", "/chats"): f"chats: {_NO_ACTION}",
        ("POST", "/groups"): f"creating a group: {_NO_ACTION}",
        ("*", "/groups/**"): "group writes: group.member does not show the right to change a group or post to it, and there is no owner action",
        ("POST", "/teams"): f"creating a team: {_NO_ACTION}",
        ("POST", "/teams/{team}/clone"): "creates a new team",
        ("*", "/teams/{team}/completeMigration"): "message migration is an administrator's job",
        ("*", "/teams/{team}/channels/{channel}/startMigration"): "message migration is an administrator's job",
        ("*", "/teams/{team}/channels/{channel}/completeMigration"): "message migration is an administrator's job",
        ("POST", "/teams/{team}/operations"): "operations are the service's own records",
        ("*", "/teams/{team}/operations/**"): "operations are the service's own records",
        ("POST", "/teams/{team}/permissionGrants"): "resource-specific consent: an administrator's job",
        ("*", "/teams/{team}/permissionGrants/**"): "resource-specific consent: an administrator's job",
        ("*", "/teams/{team}/schedule"): "Shifts has its own roles, which the integration does not read",
        ("*", "/teams/{team}/schedule/**"): "Shifts has its own roles, which the integration does not read",
        ("POST", "/teams/{team}/sendActivityNotification"): "notifies arbitrary recipients",
        ("*", "/teams/{team}/primaryChannel"): "address the channel by id: /teams/{team-id}/channels/{channel-id}",
        ("*", "/teams/{team}/primaryChannel/**"): "address the channel by id: /teams/{team-id}/channels/{channel-id}",
        ("*", "/teams/{team}/channels/{channel}/filesFolder/**"): "address the folder's files as /drives/{drive-id}/items/{item-id}",
        ("*", "/teams/{team}/channels/{channel}/messages/{message}/hostedContents"): "hosted content is written with its message",
        ("*", "/teams/{team}/channels/{channel}/messages/{message}/hostedContents/**"): "hosted content is written with its message",
        ("*", "/teams/{team}/channels/{channel}/messages/{message}/replies/{reply}/hostedContents"): "hosted content is written with its message",
        ("*", "/teams/{team}/channels/{channel}/messages/{message}/replies/{reply}/hostedContents/**"): "hosted content is written with its message",
        ("POST", "/drives"): f"creating a drive: {_NO_ACTION}",
        ("*", "/drives/{drive}"): f"a drive's own settings: {_NO_ACTION}",
        ("POST", "/drives/{drive}/items"): "create items below a folder: /items/{parent-id}/children",
        ("*", "/drives/{drive}/bundles/**"): f"bundles span items: {_NO_ACTION}",
        ("POST", "/drives/{drive}/bundles"): f"bundles span items: {_NO_ACTION}",
        ("*", "/drives/{drive}/following/**"): "address the item by id",
        ("*", "/drives/{drive}/list"): f"the library's SharePoint list: {_NO_ACTION}",
        ("*", "/drives/{drive}/list/**"): f"the library's SharePoint list: {_NO_ACTION}",
        ("*", "/drives/{drive}/special/**"): "address the item by id",
        ("*", "/drives/{drive}/createdByUser/**"): "another user's settings",
        ("*", "/drives/{drive}/lastModifiedByUser/**"): "another user's settings",
        ("*", "/drives/{drive}/items/{item}/createdByUser/**"): "another user's settings",
        ("*", "/drives/{drive}/items/{item}/lastModifiedByUser/**"): "another user's settings",
        ("POST", "/drives/{drive}/items/{item}/copy"): "a copy lands in a destination folder, a second item the check would not cover",
        ("*", "/drives/{drive}/items/{item}/analytics"): "analytics are the service's own records",
        ("*", "/drives/{drive}/items/{item}/analytics/**"): "analytics are the service's own records",
        ("*", "/drives/{drive}/items/{item}/subscriptions"): "change notifications go to an endpoint toolpass does not check",
        ("*", "/drives/{drive}/items/{item}/subscriptions/**"): "change notifications go to an endpoint toolpass does not check",
        ("POST", "/drives/{drive}/items/{item}/versions"): "versions are made by editing the item",
        ("*", "/drives/{drive}/items/{item}/versions/{version}"): "versions are made by editing the item",
        ("*", "/drives/{drive}/items/{item}/versions/{version}/content"): "versions are made by editing the item",
        ("PUT", "/users/{user}/messages/{message}/$value"): "MIME content can name any sender",
        ("PUT", "/users/{user}/mailFolders/{folder}/messages/{message}/$value"): "MIME content can name any sender",
        ("PUT", "/users/{user}/mailFolders/{folder}/childFolders/{child}/messages/{message}/$value"): "MIME content can name any sender",
        ("*", "/users/{user}/calendars/**"): "a calendar by id may be another mailbox's, shared with this one; use /users/{id}/calendar or /events",
        ("*", "/users/{user}/calendarGroups/**"): "a calendar group may hold other mailboxes' calendars; use /users/{id}/calendar or /events",
        ("POST", "/users/{user}/calendarGroups"): "a calendar group may hold other mailboxes' calendars",
        ("POST", "/users/{user}/calendars"): "use /users/{id}/calendar",
        ("POST", "/users"): f"directory administration: {_NO_ACTION}",
        ("*", "/users/{user}"): f"directory administration: {_NO_ACTION}",
        ("*", "/users/{user}/**"): f"directory administration, licences, authentication methods, chats, OneNote, Planner and To Do: {_NO_ACTION}",
    },
    prefixes=(_VERSION,),
)

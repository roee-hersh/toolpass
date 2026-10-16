"""Zendesk Support REST requests (``/api/v2``), mapped to the zendesk
integration's actions.

Tickets, organizations and users are named by their numeric id in the path
(a ``.json`` suffix on the last segment is read as Zendesk reads it). A ticket
update is ``ticket.edit``, or ``ticket.comment_public`` when all it does is add
a public comment; a comment with ``"public": false`` is an internal note,
checked as ``ticket.edit`` (toolpass has no action for internal notes, so a
light agent is refused one). A comment beside property changes needs both
checks, which one request cannot carry: it is unresolved, so send them as two
requests. A user update is ``user.edit`` while it changes only profile fields;
one that touches the role, groups, organizations, suspension or anything else
is checked as ``account.admin``.

Bodies are read only as one JSON object (``Content-Type: application/json``)
under the record's key (``{"ticket": {...}}``), with no duplicate members and
no query string beside it: Rails would merge a query's ``ticket[...]`` over
the body, and parse a form body the same way. Anything else is unresolved.

Account configuration (fields, forms, groups, brands, roles, SLAs, targets and
the like) is ``account.admin``; triggers and automations are
``business_rules.manage``, macros ``macro.manage`` and views ``view.manage``,
all on ``account``. Creating tickets, bulk ticket changes, merges and spam
marking, and everything listed in ``unrouted`` are refused: the integration has
no action that covers them, or they change more than one checked object.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import replace
from typing import Any

from toolpass.core.routes import Request, Route, RouteTable
from toolpass.integrations.zendesk.actions import ID_RE

__all__ = ["PROFILE_FIELDS", "ROUTES"]

_A = "/api/v2"
_TICKET = "ticket:{ticket_id}"
_USER = "user:{user_id}"
_ORG = "organization:{organization_id}"
_ACCOUNT = "account"
_WRITES = ("POST", "PUT", "PATCH", "DELETE")

# An extractor that always answers: the fields, or _unresolved.
_Fields = Callable[[Request, Mapping[str, str]], dict[str, str]]

# User members an agent with end-user profile access may change (user.edit).
# Any other member (role, custom_role_id, organization_id, external_id,
# suspended, verified, ...) is checked as account.admin.
PROFILE_FIELDS = frozenset(
    {
        "alias",
        "details",
        "email",
        "iana_time_zone",
        "locale",
        "locale_id",
        "name",
        "notes",
        "phone",
        "photo",
        "remote_photo_url",
        "shared_phone_number",
        "signature",
        "tags",
        "time_zone",
        "user_fields",
    }
)

# Ticket members that only annotate the update's audit; they may sit beside a public comment.
_AUDIT_ONLY = frozenset({"safe_update", "updated_stamp", "macro_id", "macro_ids", "metadata"})


class _Duplicate(ValueError):
    pass


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in pairs:
        if k in out:
            raise _Duplicate(k)
        out[k] = v
    return out


def _constant(name: str) -> Any:
    raise ValueError(f"{name} is not JSON")


def _record(r: Request, key: str) -> dict[str, Any] | str:
    """The ``key`` object of a ``{key: {...}}`` JSON body, or why it cannot be read exactly."""
    if r.content_type != "application/json":
        return f"the body is read only as JSON (Content-Type: application/json), not {r.content_type or 'none'}"
    if r.query:
        return "a write whose body toolpass reads takes no query string (Zendesk merges query parameters over the body)"
    try:
        doc = json.loads((r.body or b"").decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_constant)
    except _Duplicate as e:
        return f"the body names {e} more than once"
    except (ValueError, UnicodeDecodeError):
        return "the body is not JSON"
    if not isinstance(doc, dict) or set(doc) != {key} or not isinstance(doc[key], dict):
        return f'the body must be one object under "{key}" and nothing beside it'
    rec: dict[str, Any] = doc[key]
    return rec


def _id(value: str) -> str | None:
    """A path id, without the ``.json`` suffix Zendesk ignores, when it is a plain decimal id."""
    if value.endswith(".json"):
        value = value[: -len(".json")]
    return value if ID_RE.fullmatch(value) else None


def _ids(*names: str) -> _Fields:
    """The named path ids, each a plain decimal Zendesk id."""

    def extract(r: Request, vars_: Mapping[str, str]) -> dict[str, str]:
        out: dict[str, str] = {}
        for name in names:
            v = _id(vars_.get(name, ""))
            if v is None:
                return {"_unresolved": f"{name} {vars_.get(name, '')!r} is not a Zendesk id (plain decimal)"}
            out[name] = v
        return out

    return extract


def _same_id(rec: Mapping[str, Any], path_id: str) -> bool:
    """A record's own ``id``, when given, must be the one in the path."""
    return "id" not in rec or str(rec["id"]) == path_id


def _comment_update(key: str) -> _Fields:
    """PUT /tickets/{id} and PUT /requests/{id}: a public comment alone is
    ticket.comment_public; property changes, or an internal note with them,
    ticket.edit; a public comment beside property changes is unresolved."""
    ids = _ids("ticket_id")

    def extract(r: Request, vars_: Mapping[str, str]) -> dict[str, str]:
        out = ids(r, vars_)
        if "_unresolved" in out:
            return out
        rec = _record(r, key)
        if isinstance(rec, str):
            return {"_unresolved": rec}
        if not _same_id(rec, out["ticket_id"]):
            return {"_unresolved": f"the body's {key}.id is not the ticket in the path"}
        changes = set(rec) - _AUDIT_ONLY - {"comment"}
        if "comment" not in rec:
            return out  # ticket.edit
        comment = rec["comment"]
        if not isinstance(comment, dict):
            return {"_unresolved": "the comment is not an object"}
        if "is_public" in comment:
            return {"_unresolved": "the comment says is_public; name its visibility with public (true or false) only"}
        if comment.get("author_id") is not None:
            return {"_unresolved": "the comment names its author (author_id); toolpass checks the session's user as the author"}
        public = comment.get("public", True)
        if not isinstance(public, bool):
            return {"_unresolved": "the comment's public must be true or false"}
        if not public:
            return out  # an internal note: ticket.edit, with or without property changes
        if changes:
            return {"_unresolved": f"a public comment beside {', '.join(sorted(changes))} needs two checks; send the comment and the changes as two requests"}
        return {**out, "_action": "ticket.comment_public"}

    return extract


def _user_update(r: Request, vars_: Mapping[str, str]) -> dict[str, str]:
    """PUT /users/{id}: profile fields are user.edit; anything else account.admin."""
    out = _ids("user_id")(r, vars_)
    if "_unresolved" in out:
        return out
    rec = _record(r, "user")
    if isinstance(rec, str):
        return {"_unresolved": rec}
    if not _same_id(rec, out["user_id"]):
        return {"_unresolved": "the body's user.id is not the user in the path"}
    if set(rec) - PROFILE_FIELDS:
        return {"_action": "account.admin", "_resource": _ACCOUNT}
    return out


def _org_update(r: Request, vars_: Mapping[str, str]) -> dict[str, str]:
    out = _ids("organization_id")(r, vars_)
    if "_unresolved" in out:
        return out
    rec = _record(r, "organization")
    if isinstance(rec, str):
        return {"_unresolved": rec}
    if not _same_id(rec, out["organization_id"]):
        return {"_unresolved": "the body's organization.id is not the organization in the path"}
    return out


def _tree(action: str, prefix: str, **kw: Any) -> list[Route]:
    """Every write at ``prefix`` and under it, checked as ``action`` on the account."""
    return [Route(m, p, action, _ACCOUNT, spec=False, **kw) for m in _WRITES for p in (prefix, prefix + "/{rest+}")]


# Account configuration only administrators change (or agents with a custom-role
# permission toolpass does not read, who are then refused): account.admin.
_ADMIN_TREES = (
    "account",
    "any_channel",
    "api_tokens",
    "audit_logs",
    "brand_agents",
    "brands",
    "channels/whatsapp_templates",
    "custom_objects",
    "custom_roles",
    "custom_status",
    "custom_statuses",
    "deleted_tickets",
    "deleted_users",
    "deletion_schedules",
    "dynamic_content",
    "group_memberships",
    "group_slas",
    "groups",
    "imports",
    "it_asset_management",
    "oauth/clients",
    "oauth/tokens",
    "organization_fields",
    "organization_memberships",
    "push_notification_devices",
    "queues",
    "recipient_addresses",
    "resource_collections",
    "routing",
    "sharing_agreements",
    "slas",
    "suspended_tickets",
    "targets",
    "task_list_templates",
    "ticket_fields",
    "ticket_forms",
    "user_fields",
    "workspaces",
)


def _routes() -> list[Route]:
    tid, uid, oid = _ids("ticket_id"), _ids("user_id"), _ids("organization_id")
    t = f"{_A}/tickets/{{ticket_id}}"
    u = f"{_A}/users/{{user_id}}"
    o = f"{_A}/organizations/{{organization_id}}"
    admin = "account.admin"
    lists = "a list or search, filtered by the agent's own access; results are untrusted"
    out: list[Route] = [
        # Tickets: reads.
        Route("GET", t, "ticket.view", _TICKET, body=tid),
        Route("GET", t + "/{rest+}", "ticket.view", _TICKET, body=tid, spec=False, description="comments, audits, metrics and the rest of one ticket"),
        Route("GET", f"{_A}/tickets/recent", None, "", description=lists),
        Route("GET", f"{_A}/tickets/count", None, "", description=lists),
        Route("GET", f"{_A}/tickets/show_many", None, "", description=lists),
        Route("GET", f"{_A}/tickets/autocomplete", None, "", description=lists),
        Route("GET", f"{_A}/requests/{{ticket_id}}", "ticket.view", _TICKET, body=tid, description="a request is its ticket, seen by the requester"),
        Route("GET", f"{_A}/requests/{{ticket_id}}/{{rest+}}", "ticket.view", _TICKET, body=tid, spec=False),
        Route("GET", f"{_A}/requests/ccd", None, "", description=lists),
        Route("GET", f"{_A}/requests/open", None, "", description=lists),
        Route("GET", f"{_A}/requests/solved", None, "", description=lists),
        Route("GET", f"{_A}/requests/search", None, "", description=lists),
        Route("GET", f"{_A}/search", None, "", description=lists),
        Route("GET", f"{_A}/search/count", None, "", description=lists),
        Route("GET", f"{_A}/search/export", None, "", description=lists),
        Route("GET", f"{_A}/users/me", None, "", untrusted_output=False, description="the agent's own account"),
        # Reads sent as POST.
        Route("POST", f"{_A}/users/autocomplete", None, "", effect="read", description=lists),
        Route("POST", f"{_A}/autocomplete/tags", None, "", effect="read", description="tag names"),
        Route("POST", f"{_A}/problems/autocomplete", None, "", effect="read", description=lists),
        Route("POST", f"{_A}/views/preview", None, "", effect="read", description="the tickets a view would list, filtered by the agent's own access"),
        Route("POST", f"{_A}/views/preview/count", None, "", effect="read", description="how many tickets a view would list"),
        Route("POST", f"{_A}/triggers/validate", None, "", effect="read", untrusted_output=False, description="whether a trigger definition is valid"),
        Route("POST", f"{_A}/custom_objects/{{custom_object_key}}/records/search", None, "", effect="read", description=lists),
        Route("POST", f"{_A}/it_asset_management/assets/search", None, "", effect="read", description=lists),
        # One ticket.
        Route("PUT", t, "ticket.edit", _TICKET, body=_comment_update("ticket"), sends_out=True, description="properties, or a comment; Zendesk notifies"),
        Route("DELETE", t, "ticket.delete", _TICKET, body=tid),
        Route("POST", t + "/tags", "ticket.edit", _TICKET, body=tid),
        Route("PUT", t + "/tags", "ticket.edit", _TICKET, body=tid),
        Route("DELETE", t + "/tags", "ticket.edit", _TICKET, body=tid, effect="write"),
        Route("PUT", t + "/comments/{comment_id}/make_private", "ticket.edit", _TICKET, body=tid),
        Route("PUT", t + "/audits/{ticket_audit_id}/make_private", "ticket.edit", _TICKET, body=tid),
        # Redaction removes content for good; whoever may delete the ticket may remove part of it.
        Route("PUT", t + "/comments/{comment_id}/redact", "ticket.delete", _TICKET, effect="destructive", body=tid),
        Route("PUT", t + "/comments/{comment_id}/attachments/{attachment_id}/redact", "ticket.delete", _TICKET, effect="destructive", body=tid),
        Route("PUT", f"{_A}/chat_redactions/{{ticket_id}}", "ticket.delete", _TICKET, effect="destructive", body=tid),
        Route("PUT", f"{_A}/chat_file_redactions/{{ticket_id}}", "ticket.delete", _TICKET, effect="destructive", body=tid),
        Route("POST", t + "/task_lists", "ticket.edit", _TICKET, body=tid),
        Route(
            "PUT",
            f"{_A}/requests/{{ticket_id}}",
            "ticket.edit",
            _TICKET,
            body=_comment_update("request"),
            sends_out=True,
            description="the requester's comment or status",
        ),
        Route("POST", f"{_A}/uploads", None, "", description="stores a file until a comment attaches it; the comment is checked then"),
        Route("PUT", f"{_A}/attachments/{{attachment_id}}", admin, _ACCOUNT, description="marks an attachment safe (malware scan override)"),
        # Organizations.
        Route("PUT", o, "organization.edit", _ORG, body=_org_update),
        Route("DELETE", o, "organization.edit", _ORG, body=oid),
        Route("POST", o + "/tags", "organization.edit", _ORG, body=oid),
        Route("PUT", o + "/tags", "organization.edit", _ORG, body=oid),
        Route("DELETE", o + "/tags", "organization.edit", _ORG, body=oid, effect="write"),
        Route("POST", o + "/merge", admin, _ACCOUNT, description="merges into the organization in the body"),
        Route("POST", f"{_A}/organizations", admin, _ACCOUNT),
        Route("POST", f"{_A}/organizations/create_many", admin, _ACCOUNT),
        Route("POST", f"{_A}/organizations/create_or_update", admin, _ACCOUNT),
        Route("PUT", f"{_A}/organizations/update_many", admin, _ACCOUNT),
        Route("DELETE", f"{_A}/organizations/destroy_many", admin, _ACCOUNT),
        # Users: the profile.
        Route("PUT", u, "user.edit", _USER, body=_user_update),
        Route("POST", u + "/tags", "user.edit", _USER, body=uid),
        Route("PUT", u + "/tags", "user.edit", _USER, body=uid),
        Route("DELETE", u + "/tags", "user.edit", _USER, body=uid, effect="write"),
        Route("POST", u + "/identities", "user.edit", _USER, body=uid),
        Route("PUT", u + "/identities/{user_identity_id}", "user.edit", _USER, body=uid),
        Route("DELETE", u + "/identities/{user_identity_id}", "user.edit", _USER, body=uid),
        Route("PUT", u + "/identities/{user_identity_id}/make_primary", "user.edit", _USER, body=uid),
        Route("PUT", u + "/identities/{user_identity_id}/verify", "user.edit", _USER, body=uid),
        Route("PUT", u + "/identities/{user_identity_id}/request_verification", "user.edit", _USER, body=uid, sends_out=True),
        Route("POST", f"{_A}/end_users/{{user_id}}/identities", "user.edit", _USER, body=uid),
        Route("DELETE", f"{_A}/end_users/{{user_id}}/identities/{{user_identity_id}}", "user.edit", _USER, body=uid),
        Route("PUT", f"{_A}/end_users/{{user_id}}/identities/{{user_identity_id}}/make_primary", "user.edit", _USER, body=uid),
        Route("PUT", f"{_A}/end_users/{{user_id}}/identities/{{user_identity_id}}/request_verification", "user.edit", _USER, body=uid, sends_out=True),
        # Users: access, membership and existence are an administrator's.
        Route("DELETE", u, admin, _ACCOUNT),
        Route("PUT", u + "/merge", admin, _ACCOUNT, description="merges into the user in the body"),
        Route("POST", u + "/password", admin, _ACCOUNT),
        Route("POST", u + "/suspension", admin, _ACCOUNT),
        Route("PUT", u + "/suspension", admin, _ACCOUNT),
        Route("DELETE", u + "/suspension", admin, _ACCOUNT, effect="write"),
        Route("DELETE", u + "/sessions", admin, _ACCOUNT),
        Route("DELETE", u + "/sessions/{session_id}", admin, _ACCOUNT),
        Route("POST", u + "/group_memberships", admin, _ACCOUNT),
        Route("DELETE", u + "/group_memberships/{group_membership_id}", admin, _ACCOUNT),
        Route("PUT", u + "/group_memberships/{group_membership_id}/make_default", admin, _ACCOUNT),
        Route("POST", u + "/organization_memberships", admin, _ACCOUNT),
        Route("DELETE", u + "/organization_memberships/{organization_membership_id}", admin, _ACCOUNT),
        Route("PUT", u + "/organization_memberships/{organization_membership_id}/make_default", admin, _ACCOUNT),
        Route("DELETE", u + "/organizations/{organization_id}", admin, _ACCOUNT),
        Route("PUT", u + "/organizations/{organization_id}/make_default", admin, _ACCOUNT),
        Route("POST", f"{_A}/users", admin, _ACCOUNT),
        Route("POST", f"{_A}/users/create_many", admin, _ACCOUNT),
        Route("POST", f"{_A}/users/create_or_update", admin, _ACCOUNT),
        Route("POST", f"{_A}/users/create_or_update_many", admin, _ACCOUNT),
        Route("PUT", f"{_A}/users/update_many", admin, _ACCOUNT),
        Route("DELETE", f"{_A}/users/destroy_many", admin, _ACCOUNT),
        Route("POST", f"{_A}/users/logout_many", admin, _ACCOUNT),
    ]
    for prefix in _ADMIN_TREES:
        out += _tree(admin, f"{_A}/{prefix}")
    out += _tree("business_rules.manage", f"{_A}/triggers")
    out += _tree("business_rules.manage", f"{_A}/trigger_categories")
    out += _tree("business_rules.manage", f"{_A}/automations")
    out += _tree("business_rules.manage", f"{_A}/custom_objects/{{custom_object_key}}/triggers")
    out += _tree("macro.manage", f"{_A}/macros")
    out += _tree("view.manage", f"{_A}/views")
    return _with_json_suffix(out)


def _with_json_suffix(routes: list[Route]) -> list[Route]:
    """Each route ending in a literal segment again with ``.json`` on it
    (``/api/v2/users/me.json``), as Zendesk serves both. A route ending in
    a variable needs no twin: its extractor strips the suffix from the id."""
    out = list(routes)
    for r in routes:
        last = r.path.rsplit("/", 1)[-1]
        if not last.startswith("{"):
            out.append(replace(r, path=r.path + ".json", spec=False))
    return out


def _gone(reason: str, *paths: str) -> dict[tuple[str, str], str]:
    return {(m, p): reason for p in paths for m in _WRITES}


ROUTES = RouteTable(
    "zendesk",
    _routes(),
    claims=(_A,),
    unrouted={
        ("POST", f"{_A}/tickets"): "creates a ticket: the zendesk integration has no action for creating tickets",
        ("POST", f"{_A}/tickets/create_many"): "creates tickets: the zendesk integration has no action for creating tickets",
        ("POST", f"{_A}/requests"): "creates a ticket as an end user: the zendesk integration has no action for creating tickets",
        ("POST", f"{_A}/channels/twitter/tickets"): "creates a ticket from a post: the zendesk integration has no action for creating tickets",
        ("POST", f"{_A}/channels/voice/tickets"): "creates a ticket from a call: the zendesk integration has no action for creating tickets",
        ("POST", f"{_A}/tickets/{{ticket_id}}/task_items/{{task_item_id}}/subticket"): "creates a child ticket: no action for creating tickets",
        ("PUT", f"{_A}/tickets/update_many"): "changes the tickets listed in the query or body: not checkable ticket by ticket",
        ("DELETE", f"{_A}/tickets/destroy_many"): "deletes the tickets listed in the query: not checkable ticket by ticket",
        ("PUT", f"{_A}/tickets/mark_many_as_spam"): "deletes tickets and suspends their requesters: not checkable ticket by ticket",
        ("PUT", f"{_A}/tickets/{{ticket_id}}/mark_as_spam"): "deletes the ticket and suspends its requester, a second object",
        ("POST", f"{_A}/tickets/{{ticket_id}}/merge"): "merges the tickets in the body into this one: changes several tickets, one check covers one",
        ("POST", f"{_A}/tickets/{{ticket_id}}/satisfaction_rating"): "only the requester rates a ticket, as themselves",
        ("POST", f"{_A}/tickets/{{ticket_id}}/task_items/{{task_item_id}}/task_links"): "links a task to another object named in the body",
        ("DELETE", f"{_A}/tickets/{{ticket_id}}/task_items/{{task_item_id}}/task_links/{{task_object_link_id}}"): "task links are not modelled",
        ("POST", f"{_A}/channels/voice/agents/{{agent_id}}/tickets/{{ticket_id}}/display"): "opens a ticket on another agent's screen",
        ("POST", f"{_A}/channels/voice/agents/{{agent_id}}/users/{{user_id}}/display"): "opens a user on another agent's screen",
        ("PUT", f"{_A}/comment_redactions/{{ticket_comment_id}}"): "names a comment, not its ticket; toolpass checks tickets",
        ("DELETE", f"{_A}/attachments/{{attachment_id}}"): "names an attachment, not its ticket; toolpass checks tickets",
        ("DELETE", f"{_A}/uploads/{{token}}"): "names an upload by token, not a ticket",
        ("POST", f"{_A}/accounts"): "creates a new Zendesk account",
        ("POST", f"{_A}/users/request_create"): "emails the administrators asking them to create a user",
        ("PUT", f"{_A}/users/me/settings"): "the agent's own settings, not the user's",
        ("DELETE", f"{_A}/users/me/logout"): "logs the agent's own credential out",
        ("PUT", f"{_A}/users/{{user_id}}/password"): "changes a user's own password with the old one; not for agents",
        ("POST", f"{_A}/skips"): "records that an agent skipped a ticket: not modelled",
        **_gone("ticket pins are not modelled", f"{_A}/ticket_content_pins", f"{_A}/ticket_content_pins/{{content_pin_id}}"),
        **_gone("bookmarks are the agent's own and not modelled", f"{_A}/bookmarks", f"{_A}/bookmarks/{{bookmark_id}}"),
        **_gone("saved searches are the agent's own and not modelled", f"{_A}/saved_searches", f"{_A}/saved_searches/{{id}}"),
        **_gone("approval requests are not modelled", f"{_A}/approval_requests"),
        **_gone("task lists are not modelled", f"{_A}/task_lists/**"),
        **_gone(
            "subscribes a user to an organization's notifications: not modelled", f"{_A}/organization_subscriptions", f"{_A}/organization_subscriptions/**"
        ),
    },
)

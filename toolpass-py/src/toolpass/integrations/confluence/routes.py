"""Confluence Cloud REST requests, mapped to the confluence integration's
actions: the v2 API (``/wiki/api/v2``) for pages and blog posts, the v1 API
(``/wiki/rest/api``) for spaces, creation by space key and search.

Pages and blog posts are named by their numeric content id in the v2 path,
and the action follows the path: ``page.update`` on ``page:<id>`` for
``PUT /pages/{id}``, ``blogpost.delete`` on ``blogpost:<id>`` for ``DELETE
/blogposts/{id}``. An update that moves the page (``parentId``), hands it to
another owner (``ownerId``) or names another id in the body is unresolved, and
so is a delete with ``purge`` (that needs the space's ``manage/content``).

Spaces are checked by key, so a request that names a space by its numeric id
(every v2 create, ``/spaces/{id}``) is refused: create pages and blog posts
with v1 ``POST /wiki/rest/api/content``, whose body names ``space.key``
(``page.create`` or ``blogpost.create`` on ``space:<KEY>``). A create under a
parent page is refused too: the parent's restrictions would need a second
check. Space settings, labels and permissions are ``space.admin``.

v1 names content by id without its type. A v1 read is checked as
``page.read`` on that id (Confluence answers for whatever the id is); a v1
write to content by id is not routed. Comments, attachments, restrictions,
folders, whiteboards and the other content types are not modelled by the
integration's actions, so their writes are not routed either.

Bodies are read only as one JSON object (``Content-Type: application/json``)
with no duplicate members.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any

from toolpass.core.routes import Extractor, Request, Route, RouteTable
from toolpass.integrations.confluence.actions import CONTENT_ID_RE, SPACE_KEY_RE

__all__ = ["ROUTES", "V1", "V1_CLAIMS", "V1_UNROUTED", "V2", "V2_CLAIMS", "V2_UNROUTED"]

V1 = "/wiki/rest/api"
V2 = "/wiki/api/v2"
V1_CLAIMS = (V1,)
V2_CLAIMS = (V2,)

# The members a v1 create may carry; any other (ancestors, container,
# restrictions, id, ...) acts on more than the space toolpass checks.
_CREATE_FIELDS = frozenset({"type", "title", "space", "body", "status", "metadata"})


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


def _object(r: Request) -> dict[str, Any] | str:
    """The body as one JSON object, or why it cannot be read exactly."""
    if r.content_type != "application/json":
        return f"the body is read only as JSON (Content-Type: application/json), not {r.content_type or 'none'}"
    try:
        doc = json.loads((r.body or b"").decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_constant)
    except _Duplicate as e:
        return f"the body names {e} more than once"
    except (ValueError, UnicodeDecodeError):
        return "the body is not JSON"
    if not isinstance(doc, dict):
        return "the body is not a JSON object"
    return doc


def _content(r: Request, vars_: Mapping[str, str]) -> dict[str, str]:
    """The content id in the path must be numeric, as the integration checks it."""
    cid = vars_.get("id", "")
    if not CONTENT_ID_RE.fullmatch(cid):
        return {"_unresolved": f"content id {cid!r} is not a numeric Confluence content id"}
    return {}


def _space(r: Request, vars_: Mapping[str, str]) -> dict[str, str]:
    key = vars_.get("spaceKey", "")
    if not SPACE_KEY_RE.fullmatch(key):
        return {"_unresolved": f"space key {key!r} is not a Confluence space key"}
    return {}


def _with_body(check: Callable[[dict[str, Any]], dict[str, str]]) -> Extractor:
    """The path's content id, then ``check`` on the JSON body, whose own
    ``id``, when given, must be the one in the path."""

    def extract(r: Request, vars_: Mapping[str, str]) -> dict[str, str]:
        out = _content(r, vars_)
        if out:
            return out
        doc = _object(r)
        if isinstance(doc, str):
            return {"_unresolved": doc}
        if "id" in doc and str(doc["id"]) != vars_["id"]:
            return {"_unresolved": "the body's id is not the content in the path"}
        return check(doc)

    return extract


def _not_moved(doc: dict[str, Any]) -> dict[str, str]:
    """PUT /pages/{id}, /blogposts/{id}: an edit, not a move or a hand-over."""
    moved = sorted(k for k in ("parentId", "ownerId") if k in doc)
    if moved:
        return {"_unresolved": f"{' and '.join(moved)} moves the content or hands it to someone else; toolpass checks the edit only, so it is not routed"}
    return {}


def _any(doc: dict[str, Any]) -> dict[str, str]:
    return {}


def _delete(r: Request, vars_: Mapping[str, str]) -> dict[str, str]:
    """DELETE /pages/{id}, /blogposts/{id}: trashing it, or discarding a draft; never a purge."""
    out = _content(r, vars_)
    if out:
        return out
    if "purge" in r.params():
        return {"_unresolved": "a purge needs the space's manage/content permission, which toolpass cannot check by content id"}
    return {}


def _create(r: Request, vars_: Mapping[str, str]) -> dict[str, str]:
    """v1 POST /content: a page or blog post at the top of the space named by space.key."""
    doc = _object(r)
    if isinstance(doc, str):
        return {"_unresolved": doc}
    kind = doc.get("type")
    if kind not in ("page", "blogpost"):
        return {"_unresolved": "a create is routed for type page or blogpost; a comment or attachment is checked by its container, which is not routed"}
    space = doc.get("space")
    # The key alone: Confluence would take an id given beside it.
    if not isinstance(space, dict) or set(space) != {"key"} or not isinstance(space["key"], str) or not SPACE_KEY_RE.fullmatch(space["key"]):
        return {"_unresolved": "the request does not name the space by its key alone (space.key)"}
    if doc.get("ancestors") not in (None, []):
        return {"_unresolved": "a create under a parent page needs the parent checked as well as the space; create at the top of the space"}
    extra = sorted(set(doc) - _CREATE_FIELDS - {"ancestors"})
    if extra:
        return {"_unresolved": f"{', '.join(extra)} acts on more than the space toolpass checks; not routed"}
    return {"_action": f"{kind}.create", "spaceKey": space["key"]}


def _content_routes(kind: str, plural: str) -> list[Route]:
    """The v2 routes of pages or blog posts: one content id in the path."""
    base = f"{V2}/{plural}/{{id}}"
    res = f"{kind}:{{id}}"
    read, update, delete = f"{kind}.read", f"{kind}.update", f"{kind}.delete"
    return [
        Route("GET", base, read, res, body=_content),
        Route(
            "GET",
            base + "/{rest+}",
            read,
            res,
            body=_content,
            spec=False,
            description=f"everything under one {kind}; child pages are listed as the agent sees them",
        ),
        Route("PUT", base, update, res, body=_with_body(_not_moved)),
        Route("DELETE", base, delete, res, body=_delete),
        Route("PUT", base + "/classification-level", update, res, body=_content),
        Route("POST", base + "/classification-level/reset", update, res, body=_content),
        Route("POST", base + "/properties", update, res, body=_content),
        Route("PUT", base + "/properties/{property}", update, res, body=_content),
        Route("DELETE", base + "/properties/{property}", update, res, body=_content),
    ]


def _routes() -> list[Route]:
    sp = f"{V1}/space/{{spaceKey}}"
    lists = "a search or list, filtered by the agent's own access; results are untrusted"
    admin = "space.admin"
    return [
        # v2: pages and blog posts.
        *_content_routes("page", "pages"),
        Route("PUT", f"{V2}/pages/{{id}}/title", "page.update", "page:{id}", body=_with_body(_any)),
        *_content_routes("blogpost", "blogposts"),
        # v2 reads sent as POST.
        Route("POST", f"{V2}/content/convert-ids-to-types", None, "", effect="read", untrusted_output=False, description="the type of each content id"),
        Route("POST", f"{V2}/users-bulk", None, "", effect="read", description="user profiles, as the agent sees them"),
        Route(
            "POST", f"{V2}/user/access/check-access-by-email", None, "", effect="read", untrusted_output=False, description="whether addresses have site access"
        ),
        # v1: creating pages and blog posts by space key.
        Route("POST", f"{V1}/content", "page.create", "space:{spaceKey}", body=_create, spec=False),
        # v1: content by id, read as page.read on that id.
        Route(
            "GET",
            f"{V1}/content/{{id}}",
            "page.read",
            "page:{id}",
            body=_content,
            spec=False,
            description="v1 does not say the type; checked on the content id",
        ),
        Route("GET", f"{V1}/content/{{id}}/{{rest+}}", "page.read", "page:{id}", body=_content, spec=False),
        Route(
            "POST", f"{V1}/content/{{id}}/permission/check", None, "", effect="read", untrusted_output=False, description="what someone may do on a content id"
        ),
        Route("GET", f"{V1}/content/search", None, "", description=lists),
        Route("GET", f"{V1}/search", None, "", description=lists),
        Route("GET", f"{V1}/search/user", None, "", description=lists),
        Route("GET", f"{V1}/user/current", None, "", untrusted_output=False, description="the agent's own account"),
        # v1: spaces by key.
        Route("GET", sp, "space.read", "space:{spaceKey}", body=_space, spec=False),
        Route("GET", sp + "/{rest+}", "space.read", "space:{spaceKey}", body=_space, spec=False),
        Route("PUT", sp, admin, "space:{spaceKey}", body=_space),
        Route("DELETE", sp, admin, "space:{spaceKey}", body=_space),
        Route("POST", sp + "/label", admin, "space:{spaceKey}", body=_space),
        Route("DELETE", sp + "/label", admin, "space:{spaceKey}", body=_space),
        Route("POST", sp + "/permission", admin, "space:{spaceKey}", body=_space),
        Route("POST", sp + "/permission/custom-content", admin, "space:{spaceKey}", body=_space),
        Route("DELETE", sp + "/permission/{id}", admin, "space:{spaceKey}", body=_space),
        Route("PUT", sp + "/settings", admin, "space:{spaceKey}", body=_space),
        Route("PUT", sp + "/theme", admin, "space:{spaceKey}", body=_space),
        Route("DELETE", sp + "/theme", admin, "space:{spaceKey}", body=_space),
    ]


def _all(reason: str, *paths: str) -> dict[tuple[str, str], str]:
    return {("*", p): reason for p in paths}


_BY_ID = "v1 names the content by id without saying whether it is a page or a blog post; use the v2 /pages or /blogposts path"
_RESTRICT = "content restrictions are checked per space (page.restrict) and the request names only the content"

V1_UNROUTED: dict[tuple[str, str], str] = {
    ("POST", f"{V1}/content/archive"): "archives the pages listed in the body: no action for archiving",
    **_all("publishes a blueprint draft into a space named in the body: not routed", f"{V1}/content/blueprint/**"),
    ("POST", f"{V1}/content/{{id}}/copy"): "copies the content to a destination in the body, a second target",
    ("POST", f"{V1}/content/{{id}}/pagehierarchy/copy"): "copies a page tree to a destination in the body, a second target",
    ("PUT", f"{V1}/content/{{id}}/move/{{position}}/{{targetId}}"): "moves the page relative to a second page",
    ("DELETE", f"{V1}/content/{{id}}/pageTree"): "deletes the page's descendants too: one check covers one page",
    ("*", f"{V1}/content/{{id}}/restriction"): _RESTRICT,
    ("*", f"{V1}/content/{{id}}/restriction/**"): _RESTRICT,
    ("*", f"{V1}/content/{{id}}/child/attachment"): "attachments are checked per space (attachment.create) and the request names only the content",
    ("*", f"{V1}/content/{{id}}/child/attachment/**"): "attachments are checked per space (attachment.create) and the request names only the content",
    ("*", f"{V1}/content/{{id}}"): _BY_ID,
    ("*", f"{V1}/content/{{id}}/label"): _BY_ID,
    ("*", f"{V1}/content/{{id}}/label/{{label}}"): _BY_ID,
    ("*", f"{V1}/content/{{id}}/state"): _BY_ID,
    ("*", f"{V1}/content/{{id}}/version"): _BY_ID,
    ("*", f"{V1}/content/{{id}}/version/{{versionNumber}}"): _BY_ID,
    **_all("renders content bodies, macros included: not routed", f"{V1}/contentbody/**"),
    ("POST", f"{V1}/space"): "creating a space is a site permission the integration does not check",
    ("POST", f"{V1}/space/_private"): "creating a space is a site permission the integration does not check",
    **_all("watches act for the agent or for a user named in the query: not modelled", f"{V1}/user/watch/**"),
    **_all("user properties are not modelled", f"{V1}/user/{{userId}}/property/**"),
    **_all("groups are site administration, done outside agents", f"{V1}/group", f"{V1}/group/**"),
    **_all("the audit log is site administration, done outside agents", f"{V1}/audit", f"{V1}/audit/**"),
    **_all("site look and feel is site administration, done outside agents", f"{V1}/settings/**"),
    **_all("templates are not modelled", f"{V1}/template", f"{V1}/template/**"),
    **_all("content relations are not modelled", f"{V1}/relation/**"),
}

_SPACE_BY_ID = "names the space by numeric id; toolpass checks spaces by key"

V2_UNROUTED: dict[tuple[str, str], str] = {
    ("POST", f"{V2}/pages"): _SPACE_BY_ID + "; create pages with POST /wiki/rest/api/content and space.key",
    ("POST", f"{V2}/blogposts"): _SPACE_BY_ID + "; create blog posts with POST /wiki/rest/api/content and space.key",
    ("*", f"{V2}/pages/{{id}}/permissions"): _RESTRICT,
    ("*", f"{V2}/pages/{{id}}/restriction-state"): _RESTRICT,
    ("*", f"{V2}/blogposts/{{id}}/permissions"): _RESTRICT,
    ("*", f"{V2}/blogposts/{{id}}/restriction-state"): _RESTRICT,
    ("POST", f"{V2}/pages/{{id}}/redact"): "redaction is an Atlassian Guard feature with no action in the integration",
    ("POST", f"{V2}/blogposts/{{id}}/redact"): "redaction is an Atlassian Guard feature with no action in the integration",
    **_all(
        "comments are checked per space (comment.create) and name only their page; the integration has no comment resource",
        f"{V2}/footer-comments",
        f"{V2}/footer-comments/**",
        f"{V2}/inline-comments",
        f"{V2}/inline-comments/**",
        f"{V2}/comments/**",
    ),
    **_all("the integration has no attachment resource", f"{V2}/attachments/**"),
    **_all("custom content is not modelled", f"{V2}/custom-content", f"{V2}/custom-content/**"),
    **_all("databases are not modelled", f"{V2}/databases", f"{V2}/databases/**"),
    **_all("embeds are not modelled", f"{V2}/embeds", f"{V2}/embeds/**"),
    **_all("folders are not modelled", f"{V2}/folders", f"{V2}/folders/**"),
    **_all("whiteboards are not modelled", f"{V2}/whiteboards", f"{V2}/whiteboards/**"),
    **_all(_SPACE_BY_ID, f"{V2}/spaces", f"{V2}/spaces/**"),
    **_all(
        "space permissions and roles are space administration by numeric id; toolpass checks spaces by key",
        f"{V2}/space-permissions/**",
        f"{V2}/space-roles",
        f"{V2}/space-roles/**",
    ),
    **_all("tasks are not modelled", f"{V2}/tasks/**"),
    **_all("the admin key and app properties are administration, done outside agents", f"{V2}/admin-key", f"{V2}/app/**"),
    ("POST", f"{V2}/user/access/invite-by-email"): "invites people to the site by email",
}

ROUTES = RouteTable(
    "confluence",
    _routes(),
    claims=(*V1_CLAIMS, *V2_CLAIMS),
    unrouted={**V1_UNROUTED, **V2_UNROUTED},
)

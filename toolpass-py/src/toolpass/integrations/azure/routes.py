"""Azure Resource Manager requests, mapped to the Azure RBAC operation ARM
authorizes them as, on the scope they touch.

ARM paths name what they act on:

    /subscriptions/{s}[/resourceGroups/{rg}][/providers/{ns}/{type}/{name}[/{type}/{name}...]][/{type} | /{action}]
    /providers/Microsoft.Management/managementGroups/{mg}/...

and the operation follows from the path and the method, as ARM derives it:
``{ns}/{type}[/{type}...]/read`` for GET and HEAD, ``/write`` for PUT and
PATCH, ``/delete`` for DELETE, ``/{action}/action`` for a POST to
``.../{name}/{action}``. A GET of a collection is the type's read on its
parent. Without a provider segment the operation is Microsoft.Resources'
(``Microsoft.Resources/subscriptions/resourceGroups/delete``); an extension
resource (``{resource}/providers/Microsoft.Authorization/roleAssignments/{id}``)
is its own provider's. The request is checked as ``raw:<operation>`` on the
narrowest scope the integration takes: ``resource:<id>`` inside a resource
group, ``resourcegroup:``, ``subscription:`` or ``managementgroup:`` for
those themselves. An object directly under a subscription or management
group (a role assignment there, a subscription-level deployment) is checked
on that subscription or management group: a role or deny assignment on the
object alone is not seen. A DNS record set named ``@`` (the apex) or ``*``
(a wildcard) is checked on its zone, likewise.

Refused: a path that does not parse exactly (percent-encoded characters,
dot or empty segments, a subscription that is not a GUID), the batch
endpoint, tenant-level writes (``/providers/Microsoft.Authorization/elevateAccess``),
resource moves, creating or changing a template deployment or deployment
stack however the path spells it (``.../providers/Microsoft.Resources/deployments/{d}``
or ``.../resourcegroups/{rg}/deployments/{d}``: the template can create
anything, anywhere, as the agent), and a
write whose JSON body names another Azure resource by id: ARM checks the
linked permission (joining a subnet, attaching a disk, assigning an
identity) as the agent, and one request is checked against one scope here.
Tenant-level listings (subscriptions, tenants, management groups) list only
what the agent can see, and provider metadata is public: they are not checked. A POST action may run something that
sends data out (a Logic App run, a notification), so every action is treated
as sending out. The data planes (blob storage, Key Vault) are other hosts
and not routed.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

from toolpass.core.catalog import parse_resource
from toolpass.core.routes import Request, Route, RouteTable
from toolpass.integrations.azure.actions import GUID_RE, dots, parse_target

__all__ = ["ROUTES"]

# Every segment of a path the table reads: what ARM allows in resource
# group, resource type and resource names, all of it safe unescaped.
_SEGMENT_RE = re.compile(r"[A-Za-z0-9_.()-]{1,260}")
# A DNS record set's name may also be the apex (@) or a wildcard (*, *.name),
# in .../providers/Microsoft.Network/{dnsZones|privateDnsZones}/{zone}/{type}/{name}.
_RECORD_NAME_RE = re.compile(r"@|\*(\.[A-Za-z0-9_-]{1,63})*")
_DNS_ZONES = ("dnszones", "privatednszones")
_RECORD_TYPES = frozenset(["a", "aaaa", "caa", "cname", "ds", "mx", "naptr", "ns", "ptr", "soa", "srv", "tlsa", "txt"])
_NS_RE = re.compile(r"[A-Za-z][A-Za-z0-9]*(\.[A-Za-z0-9]+)+")
_ROLE_DEFINITION_RE = re.compile(
    r"(/subscriptions/[0-9a-f-]{36}(/resourcegroups/[^/]+)?)?/providers/microsoft\.authorization/roledefinitions/[0-9a-f-]{36}",
)
_VERBS = {"GET": "read", "HEAD": "read", "PUT": "write", "PATCH": "write", "DELETE": "delete"}
_EFFECTS = {"read": "read", "write": "write", "delete": "destructive", "action": "write"}
# Operations whose body moves or creates resources the path does not name.
_REFUSED = {
    "microsoft.resources/subscriptions/resourcegroups/moveresources/action": "a move takes resources to the resource group named in the body",
    "microsoft.resources/subscriptions/resourcegroups/validatemoveresources/action": "a move takes resources to the resource group named in the body",
    "microsoft.management/managementgroups/subscriptions/write": "moving a subscription between management groups changes its old parent too",
    "microsoft.management/managementgroups/subscriptions/delete": "moving a subscription between management groups changes its old parent too",
}
# Writes of these types, however the path spells them (with or without
# /providers/Microsoft.Resources), run a template that creates anything.
_TEMPLATES = {
    "deployments": "a deployment's template creates resources the request does not name, as the agent",
    "deploymentstacks": "a deployment stack's template creates resources the request does not name, as the agent",
}
_TEMPLATE_NS = ("microsoft.resources", "microsoft.management")


class _Base:
    """Where a path starts: a subscription, a resource group or a management
    group, with its id, the resource string the integration takes for it,
    and the operation prefix of what lies directly under it."""

    def __init__(self, ident: str, resource: str, op: str) -> None:
        self.ident, self.resource, self.op = ident, resource, op


def _unresolved(reason: str) -> dict[str, str]:
    return {"_unresolved": reason}


def _arm(r: Request, vars_: Mapping[str, str]) -> dict[str, str]:
    method = r.method.upper()
    if "x-http-method-override" in r.headers or "x-http-method" in r.headers:
        return _unresolved("a method override header is not accepted")
    path = r.raw_path
    if "%" in path:
        return _unresolved("an ARM path with percent-encoded characters is not routed; resource names need none")
    segs = path.split("/")[1:]
    low = [s.lower() for s in segs]
    for i, s in enumerate(segs):
        if not (_SEGMENT_RE.fullmatch(s) or _record_name(low, i)) or dots(s):
            return _unresolved(f"the path segment {s!r} is not an ARM name")
    if low[0] == "subscriptions":
        if len(segs) == 1:
            return _tenant(method, low)
        sub = low[1]
        if not GUID_RE.fullmatch(sub):
            return _unresolved(f"subscription {segs[1]!r} is not a subscription id (a GUID)")
        if len(segs) >= 4 and low[2] == "resourcegroups":
            rg = segs[3]
            base = _Base(f"/subscriptions/{sub}/resourceGroups/{rg}", f"resourcegroup:{sub}/{rg}", "Microsoft.Resources/subscriptions/resourceGroups")
            rest = segs[4:]
        else:
            base = _Base(f"/subscriptions/{sub}", f"subscription:{sub}", "Microsoft.Resources/subscriptions")
            rest = segs[2:]
    elif low[0] == "providers":
        if len(segs) >= 4 and low[1] == "microsoft.management" and low[2] == "managementgroups":
            mg = segs[3]
            base = _Base(f"/providers/Microsoft.Management/managementGroups/{mg}", f"managementgroup:{mg}", "Microsoft.Management/managementGroups")
            rest = segs[4:]
        else:
            return _tenant(method, low)
    elif low == ["tenants"]:
        return _tenant(method, low)
    else:
        return _unresolved(f"/{segs[0]} is not under /subscriptions or /providers (the batch endpoint is not routed)")
    return _resolve(r, method, base, rest)


def _record_name(low: list[str], i: int) -> bool:
    """Whether segment i is a DNS record set's apex or wildcard name."""
    return (
        i >= 5
        and low[i - 5] == "providers"
        and low[i - 4] == "microsoft.network"
        and low[i - 3] in _DNS_ZONES
        and low[i - 1] in _RECORD_TYPES
        and _RECORD_NAME_RE.fullmatch(low[i]) is not None
    )


def _tenant(method: str, low: list[str]) -> dict[str, str]:
    """A path at the tenant root: the listings ARM filters by the agent's own
    access and provider metadata are read unchecked; anything else there is
    at a scope the integration does not check."""
    path = "/" + "/".join(low)
    listing = low in (["subscriptions"], ["tenants"], ["providers"], ["providers", "microsoft.management", "managementgroups"])
    metadata = low[0] == "providers" and (len(low) == 2 or (len(low) == 3 and low[2] == "operations"))
    if method in ("GET", "HEAD") and (listing or metadata):
        return {"_action": "", "_resource": "", "_effect": "read", "operation": path}
    return _unresolved(f"{path} is at the tenant root, a scope the azure integration does not check")


def _resolve(r: Request, method: str, base: _Base, rest: list[str]) -> dict[str, str]:
    """The operation and scope of everything below the base. ``parent`` is
    the object the last provider's resources hang from, ``container`` the
    path their type/name pairs continue."""
    reads_providers = base.resource.startswith("subscription:") and method in ("GET", "HEAD")
    if not rest:
        verb = _VERBS.get(method)
        if verb is None:
            return _unresolved(f"{method} on {base.ident} is not an ARM operation")
        return _done(r, base, base.op, verb, base.ident)
    if rest[0].lower() == "providers":
        if len(rest) == 1 and reads_providers:
            return _done(r, base, "Microsoft.Resources/subscriptions/providers", "read", base.ident)
        chunks = _chunks(rest)
        if chunks is None:
            return _unresolved("the path's provider segments do not name a resource")
        parent = base.ident + "".join(f"/providers/{ns}/" + "/".join(pairs) for ns, pairs in chunks[:-1])
        prefix, segs = chunks[-1]
        container = f"{parent}/providers/{prefix}"
        if not segs:
            if len(chunks) == 1 and reads_providers:
                return _done(r, base, "Microsoft.Resources/subscriptions/providers", "read", base.ident)
            return _unresolved(f"{prefix} names a resource provider, not a resource")
    else:
        if any(s.lower() == "providers" for s in rest[0::2]):
            return _unresolved("the path's provider segments do not name a resource")
        parent, container, prefix, segs = base.ident, base.ident, base.op, rest
    types = segs[0::2]
    if len(segs) % 2 == 0:
        verb = _VERBS.get(method)
        if verb is None:
            return _unresolved(f"a {method} to a resource must name an action after it")
        return _done(r, base, prefix + "/" + "/".join(types), verb, container + "/" + "/".join(segs))
    head, last = segs[:-1], segs[-1]
    owner = container + "/" + "/".join(head) if head else parent
    op = prefix + "".join("/" + t for t in types)
    if method in ("GET", "HEAD"):
        return _done(r, base, op, "read", owner)
    if method == "POST":
        return _done(r, base, op, "action", owner)
    return _unresolved(f"a {method} must name the {last} it changes")


def _chunks(rest: list[str]) -> list[tuple[str, list[str]]] | None:
    """``providers/{ns}/{type}/{name}...[/providers/{ns}/...]`` split at each
    provider: every chunk but the last names a resource (type/name pairs)."""
    out: list[tuple[str, list[str]]] = []
    i = 0
    while i < len(rest):
        if rest[i].lower() != "providers" or i + 1 >= len(rest) or not _NS_RE.fullmatch(rest[i + 1]):
            return None
        ns, i = rest[i + 1], i + 2
        segs: list[str] = []
        while i < len(rest) and not (len(segs) % 2 == 0 and rest[i].lower() == "providers"):
            segs.append(rest[i])
            i += 1
        out.append((ns, segs))
    if any(not segs or len(segs) % 2 for _, segs in out[:-1]):
        return None
    return out


def _done(r: Request, base: _Base, op_prefix: str, verb: str, target: str) -> dict[str, str]:
    op = f"{op_prefix}/{verb}" if verb != "action" else f"{op_prefix}/action"
    refused = _REFUSED.get(op.lower())
    if refused is not None:
        return _unresolved(refused)
    types = op_prefix.lower().split("/")
    if verb == "write" and types[0] in _TEMPLATE_NS and types[-1] in _TEMPLATES:
        return _unresolved(_TEMPLATES[types[-1]])
    if verb == "action":
        last = op_prefix.rsplit("/", 1)[-1].lower()
        effect = "destructive" if last.startswith("delete") or last == "purge" else "write"
    else:
        effect = _EFFECTS[verb]
    resource = _scope(base, _record_set_owner(target))
    action = "raw:" + op
    try:
        parse_target(action, parse_resource(resource))
    except Exception:  # noqa: BLE001 - whatever the integration's parser refuses, the table does not send
        return _unresolved(f"{op} on {target} is not an operation and scope the azure integration can check")
    if r.method.upper() not in ("GET", "HEAD"):
        problem = _body_problem(r, target, op)
        if problem is not None:
            return _unresolved(problem)
    return {"_action": action, "_resource": resource, "_effect": effect, "operation": op}


def _record_set_owner(target: str) -> str:
    """A DNS record set named @ or * is checked on its zone: the integration's
    resource ids take neither character, and the zone's grants and denies
    are the record set's too (an assignment on the record set alone is not
    seen). Any other target is itself."""
    segs = target.split("/")
    low = [s.lower() for s in segs]
    if len(segs) >= 6 and _record_name(low, len(segs) - 1) and not _SEGMENT_RE.fullmatch(segs[-1]):
        return "/".join(segs[:-2])
    return target


def _scope(base: _Base, target: str) -> str:
    """resource:<id> for a provider resource inside a resource group, else the
    base the target sits in."""
    if base.resource.startswith("resourcegroup:") and target.lower().startswith(base.ident.lower() + "/providers/"):
        return "resource:" + target
    return base.resource


class _Obj(list[tuple[str, Any]]):
    """A JSON object as its members in order, duplicates kept, so every
    value is seen whichever one the server keeps."""


def _body_problem(r: Request, target: str, op: str) -> str | None:
    """Why a write's body cannot go out under a check of ``target`` alone:
    it is not JSON, or it names another Azure resource by id."""
    if not r.body:
        return None
    if r.content_type != "application/json":
        return "a write's body must be application/json"
    try:
        doc = json.loads(r.body.decode("utf-8"), object_pairs_hook=_Obj)
    except (ValueError, UnicodeDecodeError):
        return "the body is not JSON"
    own = target.lower().rstrip("/")
    role_assignment = op.lower() == "microsoft.authorization/roleassignments/write"

    def foreign(s: str, where: tuple[str, ...]) -> bool:
        low = s.lower()
        if "/subscriptions/" not in low and not low.startswith("/providers/"):
            return False
        low = low.rstrip("/")
        if low == own or own.startswith(low + "/"):
            return False  # the target itself or a scope above it
        return not (role_assignment and where == ("properties", "roleDefinitionId") and _ROLE_DEFINITION_RE.fullmatch(low))

    def walk(v: Any, where: tuple[str, ...]) -> str | None:
        if isinstance(v, _Obj):
            for k, item in v:
                if foreign(k, where):
                    return k
                found = walk(item, (*where, k))
                if found is not None:
                    return found
        elif isinstance(v, list):
            for item in v:
                found = walk(item, (*where, "[]"))
                if found is not None:
                    return found
        elif isinstance(v, str) and foreign(v, where):
            return v
        return None

    other = walk(doc, ())
    if other is not None:
        return f"the body names another Azure resource ({other}); ARM would check the linked permission as the agent"
    return None


_METHODS = ("GET", "HEAD", "PUT", "PATCH", "DELETE", "POST")

ROUTES = RouteTable(
    "azure",
    [
        Route(
            m,
            "/**",
            "raw",
            "",
            body=_arm,
            sends_out=m == "POST",
            untrusted_output=True if m == "POST" else None,
            description="the operation and scope read from the ARM path",
            spec=False,
        )
        for m in _METHODS
    ],
    claims=("/",),
)

"""Kubernetes API requests, mapped to raw Kubernetes verbs on the resource
they touch, which the API server's own SubjectAccessReview then answers.

Kubernetes' REST paths are regular, so a few shapes cover every built-in
and custom resource:

    /api/v1[/namespaces/{ns}]/{resource}[/{name}[/{subresource}]]
    /apis/{group}/{version}[/namespaces/{ns}]/{resource}[/{name}[/{subresource}]]

The verb comes from the method and whether a name is given, as the API
server derives it: GET is get (with a name), list, or watch (``?watch=true``);
POST create; PUT update; PATCH patch; DELETE delete, or deletecollection
without a name; any other method is not routed. The action is ``raw:<verb>:<resource>[.<group>][/<sub>]``
and the resource ``namespace:<ns>?resource=...&name=...`` or
``cluster?resource=...``. Exec, attach and port-forward are create, a
write, whatever the method. Proxying through the API server (``.../proxy/...``)
is not routed: what it reaches is not a Kubernetes resource.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

from toolpass.core.routes import Request, Route, RouteTable
from toolpass.integrations.kubernetes.actions import GROUP_RE, NAME_RE, NAMESPACE_RE, RESOURCE_RE

__all__ = ["ROUTES"]

_STREAMS = frozenset({"exec", "attach", "portforward"})
# Subresources of the Namespace object itself, at /api/v1/namespaces/{name}/<sub>.
_NAMESPACE_SUBRESOURCES = frozenset({"status", "finalize"})


_METHODS = ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE")


def _verb(method: str, named: bool, sub: str, r: Request) -> str:
    if sub in _STREAMS:
        # Exec, attach and port-forward run something in the pod whatever the method (the
        # WebSocket form is a GET); the API server authorizes them as create since 1.30.
        return "create"
    if method in ("GET", "HEAD"):
        if named:
            return "get"
        return "watch" if r.params().get("watch", "").lower() in ("true", "1") else "list"
    if method == "DELETE":
        return "delete" if named else "deletecollection"
    return {"POST": "create", "PUT": "update", "PATCH": "patch"}[method]


def _effect(verb: str, sub: str) -> str:
    if verb in ("get", "list", "watch"):
        return "read"
    return "destructive" if verb in ("delete", "deletecollection") else "write"


def _k8s(core: bool, namespaced: bool, depth: int) -> Callable[[Request, Mapping[str, str]], dict[str, str]]:
    """The extractor for one path shape: ``depth`` is 0 (collection), 1
    (named object) or 2 (subresource)."""

    def extract(r: Request, v: Mapping[str, str]) -> dict[str, str]:
        group = "" if core else v.get("group", "")
        ns = v.get("namespace", "") if namespaced else ""
        resource, name, sub = v.get("resource", ""), v.get("name", ""), v.get("subresource", "")
        if core and namespaced and depth == 0 and resource in _NAMESPACE_SUBRESOURCES:
            # /api/v1/namespaces/{name}/status: the Namespace object's own subresource, not a collection.
            ns, resource, name, sub, namespaced_now, named = "", "namespaces", ns, resource, False, True
        else:
            namespaced_now, named = namespaced, depth >= 1
        if resource == "watch":
            # The old /watch/ paths watch a resource across every namespace, not one object.
            return {"_unresolved": "the /watch/ paths are not routed; use ?watch=true on the collection"}
        for label, value, pattern in (("namespace", ns, NAMESPACE_RE), ("resource", resource, RESOURCE_RE), ("group", group, GROUP_RE)):
            if value and not pattern.fullmatch(value):
                return {"_unresolved": f"the {label} {value!r} is not a valid Kubernetes name"}
        if name and not NAME_RE.fullmatch(name):
            return {"_unresolved": f"the object name {name!r} is not a valid Kubernetes name"}
        if sub and not RESOURCE_RE.fullmatch(sub):
            return {"_unresolved": f"the subresource {sub!r} is not valid"}
        verb = _verb(r.method.upper(), named, sub, r)
        target = resource + (f".{group}" if group else "")
        action = f"raw:{verb}:{target}" + (f"/{sub}" if sub else "")
        query = f"resource={target}" + (f"&name={name}" if name else "") + (f"&subresource={sub}" if sub else "")
        place = f"namespace:{ns}" if namespaced_now else "cluster"
        return {"_action": action, "_effect": _effect(verb, sub), "_resource": f"{place}?{query}"}

    return extract


def _deeper(r: Request, v: Mapping[str, str]) -> dict[str, str]:
    return {"_unresolved": "a path below a subresource (a proxy, or the old /watch/ paths) does not name a Kubernetes resource toolpass can check"}


def _shapes(base: str, core: bool) -> list[Route]:
    out = []
    for namespaced in (True, False):
        root = f"{base}/namespaces/{{namespace}}" if namespaced else base
        for depth, tail in enumerate(("/{resource}", "/{resource}/{name}", "/{resource}/{name}/{subresource}")):
            out += [Route(m, root + tail, "raw", "", body=_k8s(core, namespaced, depth), spec=False) for m in _METHODS]
        out += [Route(m, root + "/{resource}/{name}/{subresource}/{rest+}", None, "", body=_deeper, spec=False) for m in _METHODS]
    return out


def _routes() -> list[Route]:
    return [
        *_shapes("/api/v1", core=True),
        *_shapes("/apis/{group}/{version}", core=False),
        # Discovery and health: no resource, nothing to check.
        *(
            Route("GET", path, None, "", untrusted_output=False, spec=False)
            for path in (
                "/version",
                "/api",
                "/api/v1",
                "/apis",
                "/apis/{group}",
                "/apis/{group}/{version}",
                "/openapi/{rest+}",
                "/healthz",
                "/livez",
                "/readyz",
            )
        ),
    ]


ROUTES = RouteTable("kubernetes", _routes(), claims=("/api/v1", "/apis"))

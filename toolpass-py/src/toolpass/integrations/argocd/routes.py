"""Argo CD REST requests (``/api/v1``), mapped to the argocd integration's
actions on the object Argo CD's RBAC checks.

An application is checked as ``applications:<project>/<name>`` (with its
namespace when one is given), but Argo CD's paths name only the application.
The request must therefore name the project too (``?project=``, or
``project`` in the body of a sync, rollback, patch or resource action):
Argo CD answers "not found" when the application is in another project than
the one named, which is what makes the project in the request the one the
change lands in. A request without it is refused.

Resource changes inside an application map to the fine-grained actions
(``app.update/<group>/<kind>/<namespace>/<name>``, ``app.delete/...``,
``app.action/<group>/<kind>/<action>``). A sync with local manifests or
another revision also needs ``override``, an update that changes the project
also needs the new project, a patch that touches the project or the metadata
may update another application, and a create with ``upsert`` may update
instead: one request is checked for one action, so these are refused.

Refused as well: listings, which Argo CD filters by the agent's own access;
ApplicationSets, clusters and repositories by name, whose project Argo CD
looks up and the request does not carry; project updates, which also need
permissions on the clusters and repositories they add; certificates and GPG
keys, which Argo CD checks with an empty object; the agent's own password and
session; exec, extensions and anything else not listed.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from typing import Any

from toolpass.core.catalog import ResourceError, parse_query
from toolpass.core.routes import Request, Route, RouteTable

from .actions import OBJ_RE, parse_pattern

__all__ = ["ROUTES"]

_V1 = "/api/v1"
# Kubernetes object names: an Application, AppProject or ApplicationSet name
# is a DNS subdomain, a namespace a DNS label.
_NAME_RE = re.compile(r"[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?")
_NAMESPACE_RE = re.compile(r"[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?")
_ACCOUNT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,252}")

# What the extractors write, for the listing (with the namespace when one is given).
_APP = "applications:{project}/{name}"
_LISTING = "a listing is filtered by the agent's own access in Argo CD, not the user's; get one object by name instead"
_BY_NAME = "Argo CD checks it under a project it looks up, which the request does not name"


class _Refused(Exception):
    """A request the table refuses, with the reason."""


Extract = Callable[[Request, Mapping[str, str]], dict[str, str]]


def _guard(fn: Extract) -> Extract:
    def extract(r: Request, v: Mapping[str, str]) -> dict[str, str]:
        try:
            return fn(r, v)
        except _Refused as e:
            return {"_unresolved": str(e)}

    return extract


def _refuse(reason: str) -> Extract:
    def extract(r: Request, v: Mapping[str, str]) -> dict[str, str]:
        return {"_unresolved": f"not routed: {reason}"}

    return extract


# -- reading a request the way Argo CD's gateway reads it -----------------------


def _query(r: Request) -> dict[str, list[str]]:
    """The query as Go's url.ParseQuery reads it; refused when it cannot be read exactly."""
    try:
        return parse_query(r.query) if r.query else {}
    except ResourceError:
        raise _Refused("the query cannot be read exactly as Argo CD reads it") from None


def _one(q: Mapping[str, list[str]], name: str) -> str:
    values = q.get(name, [])
    if len(values) > 1:
        raise _Refused(f"{name} is given more than once")
    return values[0] if values else ""


class _Obj(dict[str, Any]):
    """A JSON object, with the member names Go's encoding/json would confuse:
    it matches names case-insensitively and keeps the last of duplicates."""

    def __init__(self, pairs: list[tuple[str, Any]]) -> None:
        super().__init__(pairs)
        seen: set[str] = set()
        self.ambiguous: set[str] = set()
        for k, _ in pairs:
            f = k.casefold()
            if f in seen:
                self.ambiguous.add(f)
            seen.add(f)


def _body(r: Request) -> Any:
    """The JSON body, parsed so that no member can mean something else to Argo CD."""
    if "json" not in r.content_type:
        raise _Refused("the body must be JSON (Content-Type: application/json)")
    try:
        return json.loads((r.body or b"").decode("utf-8"), object_pairs_hook=_Obj)
    except (ValueError, UnicodeDecodeError):
        raise _Refused("the body is not a JSON document toolpass reads exactly as Argo CD does") from None


def _member(doc: Any, *path: str) -> Any:
    """The member at path (None when absent), refused when a name on the way is
    given twice or in another case, which Go's decoder would also take."""
    cur = doc
    for name in path:
        if cur is None:
            return None
        if not isinstance(cur, _Obj):
            raise _Refused(f"{'.'.join(path)}: the body does not have the expected shape")
        f = name.casefold()
        if f in cur.ambiguous:
            raise _Refused(f"{name} is given more than once in the body")
        found = [k for k in cur if k.casefold() == f]
        if found and found[0] != name:
            raise _Refused(f"the body names {found[0]!r}; write it {name!r}")
        cur = cur.get(name)
    return cur


def _str(doc: Any, *path: str) -> str:
    v = _member(doc, *path)
    if v is None:
        return ""
    if not isinstance(v, str):
        raise _Refused(f"{'.'.join(path)} must be a string")
    return v


def _check(value: str, pattern: re.Pattern[str], what: str) -> str:
    if not pattern.fullmatch(value):
        raise _Refused(f"{what} {value!r} is not a valid name")
    return value


def _app(name: str, project: str, namespace: str) -> str:
    """The application's RBAC object; the check drops Argo CD's own namespace from it."""
    _check(name, _NAME_RE, "the application")
    if project == "":
        raise _Refused("name the application's project (?project=, or project in the body): Argo CD checks applications under their project")
    _check(project, _NAME_RE, "the project")
    if namespace == "":
        return f"{project}/{name}"
    return f"{project}/{_check(namespace, _NAMESPACE_RE, 'the application namespace')}/{name}"


def _upsert(value: str) -> None:
    if value.lower() not in ("", "false", "0", "f"):
        raise _Refused("upsert may update an existing object, which needs update as well as create: create it, or update it with its own request")


# -- applications ---------------------------------------------------------------


def _app_query(kind: str = "applications", ns_param: str = "appNamespace", projects: bool = False) -> Extract:
    """A request about one application, by its name in the path and its project in the query."""

    @_guard
    def extract(r: Request, v: Mapping[str, str]) -> dict[str, str]:
        q = _query(r)
        return {"_resource": f"{kind}:{_app(v['name'], _project_param(q, projects), _one(q, ns_param))}"}

    return extract


def _project_param(q: Mapping[str, list[str]], projects: bool = False) -> str:
    """The project a query names: ``project``, one value. A Get's
    ApplicationQuery also takes ``projects`` when ``project`` is absent
    (getProjectsFromApplicationQuery); every other request ignores it."""
    values = q.get("project", [])
    if projects:
        if values and q.get("projects"):
            raise _Refused("name the project once, in project or in projects")
        values = values or q.get("projects", [])
    if len(values) > 1:
        raise _Refused("name one project")
    return values[0] if values else ""


def _resource_query(verb: str) -> Extract:
    """PatchResource and DeleteResource: the fine-grained action on the resource in the query."""

    @_guard
    def extract(r: Request, v: Mapping[str, str]) -> dict[str, str]:
        q = _query(r)
        obj = _app(v["name"], _project_param(q), _one(q, "appNamespace"))
        action = f"app.{verb}/{_one(q, 'group')}/{_one(q, 'kind')}/{_one(q, 'namespace')}/{_one(q, 'resourceName')}"
        if parse_pattern(action) is None:
            raise _Refused("the request does not name the resource exactly (group, kind, namespace, resourceName)")
        return {"_action": action, "_resource": f"applications:{obj}"}

    return extract


@_guard
def _action_query(r: Request, v: Mapping[str, str]) -> dict[str, str]:
    """RunResourceAction: the action's name is the body, a JSON string."""
    q = _query(r)
    obj = _app(v["name"], _project_param(q), _one(q, "appNamespace"))
    name = _body(r)
    if not isinstance(name, str):
        raise _Refused("the body must be the action's name, a JSON string")
    action = f"app.action/{_one(q, 'group')}/{_one(q, 'kind')}/{name}"
    if parse_pattern(action) is None:
        raise _Refused("the request does not name the resource action exactly (group, kind, action)")
    return {"_action": action, "_resource": f"applications:{obj}"}


@_guard
def _action_body(r: Request, v: Mapping[str, str]) -> dict[str, str]:
    """RunResourceActionV2: everything in the body."""
    doc = _body(r)
    obj = _app(v["name"], _str(doc, "project"), _str(doc, "appNamespace"))
    action = f"app.action/{_str(doc, 'group')}/{_str(doc, 'kind')}/{_str(doc, 'action')}"
    if parse_pattern(action) is None:
        raise _Refused("the request does not name the resource action exactly (group, kind, action)")
    return {"_resource": f"applications:{obj}", "_action": action}


def _app_body(*refused: str) -> Extract:
    """A request whose body carries the project (sync, rollback); the members
    in ``refused`` need a second action and are refused when set."""

    @_guard
    def extract(r: Request, v: Mapping[str, str]) -> dict[str, str]:
        doc = _body(r)
        for name in refused:
            if _member(doc, name) not in (None, "", [], {}):
                raise _Refused(f"a sync with {name} also needs override, and toolpass checks one action per request")
        return {"_resource": f"applications:{_app(v['name'], _str(doc, 'project'), _str(doc, 'appNamespace'))}"}

    return extract


# What a patch must not mention: Argo CD applies it, then updates the
# application the result names (metadata.name, metadata.namespace) under the
# project it names (spec.project).
_RETARGET = ("project", "metadata")


def _retargets(text: str) -> bool:
    """Whether a JSON or merge patch mentions a member that would point the
    update at another application or project, in any member name or string
    (a JSON patch's path), duplicates included, in any case."""
    hit = False

    def mentions(s: str) -> bool:
        return any(w in s.casefold() for w in _RETARGET)

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        nonlocal hit
        hit = hit or any(mentions(k) or (isinstance(x, str) and mentions(x)) for k, x in items)
        return dict(items)

    def strings(x: Any) -> bool:
        if isinstance(x, list):
            return any(strings(y) for y in x)
        if isinstance(x, dict):
            return any(strings(y) for y in x.values())
        return isinstance(x, str) and mentions(x)

    try:
        doc = json.loads(text, object_pairs_hook=pairs)
    except ValueError:
        raise _Refused("the patch is not JSON toolpass can read") from None
    return hit or strings(doc)


@_guard
def _patch(r: Request, v: Mapping[str, str]) -> dict[str, str]:
    """Patch: the project from the body; a patch that touches the project or the metadata is refused."""
    doc = _body(r)
    obj = _app(v["name"], _str(doc, "project"), _str(doc, "appNamespace"))
    if _retargets(_str(doc, "patch")):
        raise _Refused(
            "a patch that touches the project or the metadata may change which application or project is updated; change those with their own request"
        )
    return {"_resource": f"applications:{obj}"}


@_guard
def _update(r: Request, v: Mapping[str, str]) -> dict[str, str]:
    """Update: the whole application in the body, its current project in the query."""
    q = _query(r)
    doc = _body(r)
    project = _project_param(q)
    if _str(doc, "spec", "project") != project:
        raise _Refused("the body's spec.project must be the project named in ?project=; moving an application needs both projects")
    return {"_resource": f"applications:{_app(v['name'], project, _str(doc, 'metadata', 'namespace'))}"}


@_guard
def _update_spec(r: Request, v: Mapping[str, str]) -> dict[str, str]:
    """UpdateSpec: the spec in the body, the current project in the query."""
    q = _query(r)
    doc = _body(r)
    project = _project_param(q)
    if _str(doc, "project") != project:
        raise _Refused("the spec's project must be the project named in ?project=; moving an application needs both projects")
    return {"_resource": f"applications:{_app(v['name'], project, _one(q, 'appNamespace'))}"}


def _create(kind: str, project: tuple[str, ...]) -> Extract:
    """Creating an application or ApplicationSet: name, namespace and project from the body."""

    @_guard
    def extract(r: Request, v: Mapping[str, str]) -> dict[str, str]:
        _upsert(_one(_query(r), "upsert"))
        doc = _body(r)
        return {"_resource": f"{kind}:{_app(_str(doc, 'metadata', 'name'), _str(doc, *project), _str(doc, 'metadata', 'namespace'))}"}

    return extract


# -- projects, clusters, repositories, accounts ------------------------------------


def _project_path(var: str) -> Extract:
    @_guard
    def extract(r: Request, v: Mapping[str, str]) -> dict[str, str]:
        return {"_resource": f"projects:{_check(v[var], _NAME_RE, 'the project')}"}

    return extract


@_guard
def _project_create(r: Request, v: Mapping[str, str]) -> dict[str, str]:
    doc = _body(r)
    if _member(doc, "upsert") not in (None, False):
        _upsert("true")
    return {"_resource": f"projects:{_check(_str(doc, 'project', 'metadata', 'name'), _NAME_RE, 'the project')}"}


def _url(value: str, what: str) -> str:
    if not OBJ_RE.fullmatch(value):
        raise _Refused(f"the request does not name the {what} by a URL toolpass checks")
    return value


def _scoped_create(kind: str, url_field: str) -> Extract:
    """Creating a cluster or repository: its URL and project from the body, as Argo CD's RBAC object."""

    @_guard
    def extract(r: Request, v: Mapping[str, str]) -> dict[str, str]:
        _upsert(_one(_query(r), "upsert"))
        doc = _body(r)
        url = _url(_str(doc, url_field), url_field)
        project = _str(doc, "project")
        return {"_resource": f"{kind}:{_check(project, _NAME_RE, 'the project') + '/' if project else ''}{url}"}

    return extract


def _creds(kind: str, where: str) -> Extract:
    """Repository credentials, checked on their URL: from the body (create) or the path."""

    @_guard
    def extract(r: Request, v: Mapping[str, str]) -> dict[str, str]:
        if where == "body":
            _upsert(_one(_query(r), "upsert"))
            url = _str(_body(r), "url")
        else:
            url = v["url"]
        return {"_resource": f"{kind}:{_url(url, 'credentials')}"}

    return extract


@_guard
def _account(r: Request, v: Mapping[str, str]) -> dict[str, str]:
    return {"_resource": f"accounts:{_check(v['name'], _ACCOUNT_RE, 'the account')}"}


# -- the table ----------------------------------------------------------------


def _routes() -> list[Route]:
    a = f"{_V1}/applications/{{name}}"
    get = _app_query()
    out = [
        # Applications.
        Route("GET", f"{_V1}/applications", None, "", body=_refuse(_LISTING)),
        Route("POST", f"{_V1}/applications", "app.create", _APP, body=_create("applications", ("spec", "project"))),
        Route("GET", a, "app.get", _APP, body=_app_query(projects=True)),
        Route("PUT", f"{_V1}/applications/{{name}}", "app.update", _APP, body=_update),
        Route("PATCH", a, "app.update", _APP, body=_patch),
        Route("DELETE", a, "app.delete", _APP, body=get),
        Route("PUT", f"{a}/spec", "app.update", _APP, body=_update_spec),
        Route("POST", f"{a}/sync", "app.sync", _APP, body=_app_body("manifests", "revision", "revisions")),
        Route("POST", f"{a}/rollback", "app.rollback", _APP, body=_app_body()),
        Route("DELETE", f"{a}/operation", "app.sync", _APP, effect="write", body=get, description="terminate the running sync"),
        Route("POST", f"{a}/resource", "app.update", _APP, body=_resource_query("update"), description="app.update/<group>/<kind>/<namespace>/<name>"),
        Route("DELETE", f"{a}/resource", "app.delete", _APP, body=_resource_query("delete"), description="app.delete/<group>/<kind>/<namespace>/<name>"),
        # The action comes from the request: app.action/<group>/<kind>/<action>, or the request is refused.
        Route("POST", f"{a}/resource/actions", None, _APP, body=_action_query, description="app.action/<group>/<kind>/<action>"),
        Route("POST", f"{a}/resource/actions/v2", None, _APP, body=_action_body, description="app.action/<group>/<kind>/<action>"),
        Route("GET", f"{a}/logs", "logs.get", "logs:{project}/{name}", body=_app_query("logs")),
        Route("GET", f"{a}/pods/{{podName}}/logs", "logs.get", "logs:{project}/{name}", body=_app_query("logs")),
        Route("GET", f"{a}/links", "app.get", _APP, body=_app_query(ns_param="namespace")),
        Route("GET", f"{_V1}/applications/{{name}}/managed-resources", "app.get", _APP, body=get),
        Route("GET", f"{_V1}/applications/{{name}}/resource-tree", "app.get", _APP, body=get),
        Route("GET", f"{_V1}/applications/{{name}}/server-side-diff", "app.get", _APP, body=get),
        Route("GET", f"{_V1}/stream/applications", None, "", body=_refuse(_LISTING)),
        Route("GET", f"{_V1}/stream/applications/{{name}}/resource-tree", "app.get", _APP, body=get),
        Route("POST", f"{_V1}/applications/manifestsWithFiles", None, "", body=_refuse("a streamed upload toolpass does not read")),
        # ApplicationSets: only a create names the project (its template's).
        Route("GET", f"{_V1}/applicationsets", None, "", body=_refuse(_LISTING)),
        Route(
            "POST",
            f"{_V1}/applicationsets",
            "appset.create",
            "applicationsets:{project}/{name}",
            body=_create("applicationsets", ("spec", "template", "spec", "project")),
        ),
        Route("POST", f"{_V1}/applicationsets/generate", None, "", body=_refuse(_BY_NAME)),
        Route("*", f"{_V1}/applicationsets/{{name}}", None, "", body=_refuse(_BY_NAME), spec=False),
        Route("*", f"{_V1}/applicationsets/{{name}}/{{rest+}}", None, "", body=_refuse(_BY_NAME), spec=False),
        Route("GET", f"{_V1}/stream/applicationsets", None, "", body=_refuse(_LISTING)),
        # Projects.
        Route("GET", f"{_V1}/projects", None, "", body=_refuse(_LISTING)),
        Route("POST", f"{_V1}/projects", "project.create", "projects:{name}", body=_project_create),
        Route("GET", f"{_V1}/projects/{{name}}", "project.get", "projects:{name}", body=_project_path("name")),
        Route("GET", f"{_V1}/projects/{{name}}/{{rest+}}", "project.get", "projects:{name}", body=_project_path("name"), spec=False),
        Route(
            "PUT",
            f"{_V1}/projects/{{name}}",
            None,
            "",
            body=_refuse("a project update also needs the clusters and repositories it adds, which depend on the project as it is"),
        ),
        Route("DELETE", f"{_V1}/projects/{{name}}", "project.delete", "projects:{name}", body=_project_path("name")),
        Route("POST", f"{_V1}/projects/{{project}}/roles/{{role}}/token", "project.update", "projects:{project}", body=_project_path("project")),
        Route("DELETE", f"{_V1}/projects/{{project}}/roles/{{role}}/token/{{iat}}", "project.update", "projects:{project}", body=_project_path("project")),
        # Clusters and repositories: Argo CD checks one under the project it is
        # scoped to, which only a create's body says.
        Route("GET", f"{_V1}/clusters", None, "", body=_refuse(_LISTING)),
        Route("POST", f"{_V1}/clusters", "cluster.create", "clusters:{project}/{server}", body=_scoped_create("clusters", "server")),
        Route("*", f"{_V1}/clusters/{{id}}", None, "", body=_refuse(_BY_NAME), spec=False),
        Route("*", f"{_V1}/clusters/{{id}}/{{rest+}}", None, "", body=_refuse(_BY_NAME), spec=False),
    ]
    for base, kind, verb in (("repositories", "repositories", "repo"), ("write-repositories", "write_repositories", "writerepo")):
        out += [
            Route("GET", f"{_V1}/{base}", None, "", body=_refuse(_LISTING)),
            Route("POST", f"{_V1}/{base}", f"{verb}.create", kind + ":{project}/{repo}", body=_scoped_create(kind, "repo")),
            Route("*", f"{_V1}/{base}/{{repo}}", None, "", body=_refuse(_BY_NAME), spec=False),
            Route("*", f"{_V1}/{base}/{{repo}}/{{rest+}}", None, "", body=_refuse(_BY_NAME), spec=False),
        ]
    for base, kind, verb in (("repocreds", "repositories", "repo"), ("write-repocreds", "write_repositories", "writerepo")):
        out += [
            Route("GET", f"{_V1}/{base}", None, "", body=_refuse(_LISTING)),
            Route("POST", f"{_V1}/{base}", f"{verb}.create", kind + ":{url}", body=_creds(kind, "body")),
            Route(
                "PUT",
                f"{_V1}/{base}/{{url}}",
                f"{verb}.update",
                kind + ":{url}",
                body=_creds(kind, "path"),
                description="the URL in the path is the one Argo CD uses",
            ),
            Route("DELETE", f"{_V1}/{base}/{{url}}", f"{verb}.delete", kind + ":{url}", body=_creds(kind, "path")),
        ]
    out += [
        # Certificates and GPG keys: Argo CD checks them with an empty object.
        Route(
            "*",
            f"{_V1}/certificates",
            None,
            "",
            body=_refuse("Argo CD checks certificates with an empty object, which a certificates: resource cannot name"),
            spec=False,
        ),
        Route("*", f"{_V1}/gpgkeys", None, "", body=_refuse("Argo CD checks GPG keys with an empty object, which a gpgkeys: resource cannot name"), spec=False),
        Route(
            "*",
            f"{_V1}/gpgkeys/{{keyID}}",
            None,
            "",
            body=_refuse("Argo CD checks GPG keys with an empty object, which a gpgkeys: resource cannot name"),
            spec=False,
        ),
        # Accounts and the session.
        Route("GET", f"{_V1}/account", None, "", body=_refuse(_LISTING)),
        Route("GET", f"{_V1}/account/{{name}}", "account.get", "accounts:{name}", body=_account),
        Route("POST", f"{_V1}/account/{{name}}/token", "account.update", "accounts:{name}", body=_account),
        Route("DELETE", f"{_V1}/account/{{name}}/token/{{id}}", "account.update", "accounts:{name}", body=_account),
        Route("PUT", f"{_V1}/account/password", None, "", body=_refuse("changes the password of the calling account, which is the agent's")),
        Route(
            "GET", f"{_V1}/account/can-i/{{resource}}/{{action}}/{{subresource}}", None, "", untrusted_output=False, description="the agent's own permissions"
        ),
        Route("POST", f"{_V1}/session", None, "", effect="read", untrusted_output=False, description="log in; every later request is checked"),
        Route("DELETE", f"{_V1}/session", None, "", body=_refuse("ends the calling session, which is the agent's")),
        Route("GET", f"{_V1}/session/userinfo", None, "", untrusted_output=False, description="the agent's own identity"),
        Route("GET", f"{_V1}/settings", None, "", untrusted_output=False),
        Route("GET", f"{_V1}/settings/plugins", None, "", untrusted_output=False),
        Route("GET", f"{_V1}/notifications/services", None, "", untrusted_output=False),
        Route("GET", f"{_V1}/notifications/templates", None, "", untrusted_output=False),
        Route("GET", f"{_V1}/notifications/triggers", None, "", untrusted_output=False),
        Route("GET", "/api/version", None, "", untrusted_output=False),
        # Everything else under the API, reads included, and exec and extensions.
        Route("*", "/api/{rest+}", None, "", body=_refuse("toolpass has no route for it in argocd"), spec=False),
        Route("*", "/terminal", None, "", body=_refuse("exec also needs the application's get, and the project comes from the query"), spec=False),
        Route("*", "/extensions/{rest+}", None, "", body=_refuse("a proxy extension also needs the application's get"), spec=False),
    ]
    # Every other read about one application: Argo CD checks get.
    for tail in ("events", "manifests", "resource", "resource/actions", "resource/links", "syncwindows"):
        out.append(Route("GET", f"{a}/{tail}", "app.get", _APP, body=get))
    out.append(Route("GET", f"{a}/revisions/{{revision}}/{{rest+}}", "app.get", _APP, body=get, spec=False))
    return out


ROUTES = RouteTable("argocd", _routes(), claims=(_V1,))

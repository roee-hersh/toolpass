"""Databricks workspace REST requests (``/api/2.x``), mapped to the
databricks integration's actions on the workspace object or Unity Catalog
securable they name.

Workspace objects are checked by id: a job (``job_id``), a cluster
(``cluster_id``), a SQL warehouse, a pipeline, a Git folder (repo), read
from the path, the query (GET) or the JSON body (POST). Unity Catalog
securables are checked by full name from the path, in lower case as Unity
Catalog compares them, with USE_CATALOG and USE_SCHEMA on their parents as
Databricks requires; files in a volume
(``/api/2.0/fs/files/Volumes/<catalog>/<schema>/<volume>/...``) check the
volume. A body or query that names the id twice, in another spelling
(``jobId``), or in a form toolpass does not read exactly is refused.

Lists (jobs, clusters, catalogs, tables, ...) need no user check: the
workspace filters them by the agent's own access, and their results are
untrusted.

Refused, because toolpass cannot check them as one action on one object:

- anything that runs code or SQL as the agent's principal: SQL statement
  execution, the command execution API (1.2), one-time job runs
  (``jobs/runs/submit``), and the results of statements and commands;
- runs named by ``run_id`` (get, output, cancel, repair, delete): a run id
  does not say which job it belongs to; ``jobs/runs/list?job_id=`` reads a
  job's runs;
- edits Databricks checks against the caller (a job's or pipeline's
  settings and run-as identity, a cluster's policy, pool and access mode),
  and creating jobs, clusters, warehouses, catalogs, tables, views and
  functions (which would belong to the agent and read with its rights);
- the Workspace and DBFS APIs, which name objects by path while toolpass
  checks notebooks and directories by id, and secrets;
- model serving endpoints addressed by name (get, query, update, delete):
  their permissions are kept under the endpoint's id, which the request does
  not carry. Their ACLs (``/api/2.0/permissions/serving-endpoints/<id>``) are
  routed.
"""

from __future__ import annotations

import json
import re
import urllib.parse
from collections.abc import Callable, Mapping
from typing import Any

from toolpass.core.routes import Extractor, Request, Route, RouteTable
from toolpass.integrations.databricks.actions import UC_NAME_RE, UC_TYPES, WS_ID_RE, WS_TYPES

__all__ = ["ROUTES"]

_NUM_RE = re.compile(r"[1-9][0-9]{0,18}")
# The Permissions API's object types, as toolpass resource types.
_PERMISSION_TYPES = {t.object: name for name, t in WS_TYPES.items()}
# The Unity Catalog securable types toolpass checks, by the name the grants API uses.
_UC_SECURABLES = {t.securable: name for name, t in UC_TYPES.items()}

_JOBS = ("2.0", "2.1", "2.2")
_CLUSTERS = ("2.0", "2.1")
_UC = "/api/2.1/unity-catalog"


class _Unreadable(ValueError):
    pass


def _pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
    keys = [k for k, _ in items]
    if len(set(keys)) != len(keys):
        raise _Unreadable("a JSON member is given twice")
    return dict(items)


def _reject_constant(name: str) -> Any:
    raise _Unreadable(f"{name} is not JSON")


def _body(r: Request) -> dict[str, Any]:
    """The JSON object body, read strictly; {} when there is none."""
    if not r.body:
        return {}
    if r.content_type != "application/json":
        raise _Unreadable("the body is read only as JSON (Content-Type: application/json)")
    try:
        doc = json.loads(r.body.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_reject_constant)
    except _Unreadable:
        raise
    except (ValueError, UnicodeDecodeError):
        raise _Unreadable("the body is not JSON") from None
    if not isinstance(doc, dict):
        raise _Unreadable("the body is not a JSON object")
    return doc


def _norm(key: str) -> str:
    """A member name with case, underscores and other separators folded away:
    accessControlList, ACCESS-CONTROL-LIST and access_control_list are one."""
    return re.sub(r"[^0-9a-z]", "", key.lower())


def _canon(v: Any, kind: str) -> str:
    """An id in its one exact spelling: a positive integer (or its decimal
    string) for numeric ids, a workspace id or a Unity Catalog name otherwise."""
    if kind == "num":
        if isinstance(v, int) and not isinstance(v, bool) and 0 < v < 2**63:
            return str(v)
        if isinstance(v, str) and _NUM_RE.fullmatch(v) and int(v) < 2**63:
            return v
    elif kind == "ws" and isinstance(v, str) and WS_ID_RE.fullmatch(v):
        return v
    elif kind == "uc" and isinstance(v, str) and UC_NAME_RE.fullmatch(v):
        return v.lower()
    raise _Unreadable("its value is not an id toolpass reads")


def _from_body(r: Request, key: str, kind: str) -> str:
    if any(_norm(k) == _norm(key) for k in urllib.parse.parse_qs(r.query, keep_blank_values=True)):
        raise _Unreadable(f"{key} is in the query of a request that carries it in the body")
    doc = _body(r)
    if any(_norm(k) == _norm(key) and k != key for k in doc):
        raise _Unreadable(f"{key} is given in another spelling")
    if key not in doc:
        raise _Unreadable(f"the body does not carry {key}")
    try:
        return _canon(doc[key], kind)
    except _Unreadable as e:
        raise _Unreadable(f"{key}: {e}") from None


def _from_query(r: Request, key: str, kind: str) -> str:
    if r.body:
        raise _Unreadable("a GET with a body")
    query = urllib.parse.parse_qs(r.query, keep_blank_values=True)
    if any(_norm(k) == _norm(key) and k != key for k in query):
        raise _Unreadable(f"{key} is given in another spelling")
    values = query.get(key, [])
    if len(values) != 1:
        raise _Unreadable(f"the query must carry {key} once")
    try:
        return _canon(values[0], kind)
    except _Unreadable as e:
        raise _Unreadable(f"{key}: {e}") from None


def _guard(fn: Callable[[Request, Mapping[str, str]], dict[str, str]]) -> Extractor:
    def extract(r: Request, vars_: Mapping[str, str]) -> dict[str, str]:
        try:
            return fn(r, vars_)
        except _Unreadable as e:
            return {"_unresolved": str(e)}

    return extract


def _query_id(key: str, kind: str = "num") -> Extractor:
    return _guard(lambda r, v: {"id": _from_query(r, key, kind)})


def _body_id(key: str, kind: str = "num", forbid: tuple[str, ...] = ()) -> Extractor:
    """The id from the body; ``forbid`` lists members that would act beyond it."""

    def extract(r: Request, vars_: Mapping[str, str]) -> dict[str, str]:
        out = {"id": _from_body(r, key, kind)}
        doc = _body(r)
        for name in forbid:
            if any(_norm(k) == _norm(name) and doc[k] not in (None, False) for k in doc):
                raise _Unreadable(f"{name} acts beyond the one {key} toolpass checks")
        return out

    return _guard(extract)


def _path_id(kind: str = "ws") -> Extractor:
    def extract(r: Request, vars_: Mapping[str, str]) -> dict[str, str]:
        try:
            return {"id": _canon(vars_.get("id", ""), kind)}
        except _Unreadable:
            raise _Unreadable(f"{vars_.get('id', '')!r} is not an id toolpass reads") from None

    return _guard(extract)


def _refuse(reason: str) -> Extractor:
    return lambda r, vars_: {"_unresolved": reason}


# -- Unity Catalog ----------------------------------------------------------------


def _full_name(value: str, parts: int) -> str:
    names = value.split(".")
    if len(names) != parts:
        raise _Unreadable(f"{value!r} is not a name of {parts} dotted part(s)")
    return ".".join(_canon(n, "uc") for n in names)


def _uc_path(rtype: str) -> Extractor:
    """A securable named by the path's full name. A rename keeps it in its
    parent: new_name must be one plain name part, in its one spelling, so it
    cannot name another parent."""

    def extract(r: Request, vars_: Mapping[str, str]) -> dict[str, str]:
        name = _full_name(vars_.get("full_name", ""), UC_TYPES[rtype].parts)
        if r.method.upper() == "PATCH":
            doc = _body(r)
            for k, v in doc.items():
                if _norm(k) == "newname" and (k != "new_name" or not isinstance(v, str) or not UC_NAME_RE.fullmatch(v)):
                    raise _Unreadable("new_name must be a plain name in the same parent, given once")
        elif r.method.upper() in ("GET", "HEAD") and r.body:
            raise _Unreadable("a read with a body")
        return {"name": name}

    return _guard(extract)


def _uc_manage(rtype: str) -> str:
    """The action that manages a securable: MANAGE (or ownership) with the USE
    privileges on its parents, which Databricks also requires."""
    return "uc.manage" if rtype == "catalog" else f"{rtype}.manage"


def _uc_grants(r: Request, vars_: Mapping[str, str]) -> dict[str, str]:
    """/permissions/{securable_type}/{full_name}: the securable the grants are on."""
    rtype = _UC_SECURABLES.get(vars_.get("securable_type", "").lower())
    if rtype is None:
        raise _Unreadable(f"grants on a {vars_.get('securable_type', '')!r} are not checked; toolpass checks {', '.join(sorted(_UC_SECURABLES))}")
    return {"_action": _uc_manage(rtype), "_resource": f"{rtype}:{_full_name(vars_.get('full_name', ''), UC_TYPES[rtype].parts)}"}


def _uc_create(parent: str, *names: str, nested: str = "", forbid: tuple[str, ...] = ()) -> Extractor:
    """POST of a new securable: the parent (catalog or schema) from the body's
    catalog_name and schema_name; ``forbid`` lists members that reach beyond it
    (an external storage location, checked against the agent)."""

    def extract(r: Request, vars_: Mapping[str, str]) -> dict[str, str]:
        doc: Any = _body(r)
        if nested:
            if any(_norm(k) == _norm(nested) and k != nested for k in doc):
                raise _Unreadable(f"{nested} is given in another spelling")
            doc = doc.get(nested)
            if not isinstance(doc, dict):
                raise _Unreadable(f"the body does not carry {nested}")
        for name in forbid:
            if any(_norm(k) == _norm(name) and doc[k] not in (None, "") for k in doc):
                raise _Unreadable(f"{name} reaches storage outside the {parent}, which Databricks checks against the agent")
        parts = []
        for name in names:
            if any(_norm(k) == _norm(name) and k != name for k in doc):
                raise _Unreadable(f"{name} is given in another spelling")
            if name not in doc:
                raise _Unreadable(f"the body does not carry {name}")
            parts.append(_canon(doc[name], "uc"))
        return {"_resource": f"{parent}:{'.'.join(parts)}"}

    return _guard(extract)


def _volume_file(r: Request, vars_: Mapping[str, str]) -> dict[str, str]:
    """/api/2.0/fs/{files|directories}/Volumes/<catalog>/<schema>/<volume>/...:
    the volume, from the raw path so no segment can climb out of it."""
    rest = r.raw_path.partition("/api/2.0/fs/")[2].partition("/")[2]  # after files/ or directories/
    segments = []
    for raw in rest.split("/"):
        try:
            seg = urllib.parse.unquote(raw, errors="strict")
        except UnicodeDecodeError:
            raise _Unreadable("the file path is not UTF-8") from None
        if seg in ("", ".", "..") or "/" in seg or "\\" in seg:
            raise _Unreadable("the file path has an empty, . or .. segment, or an encoded slash")
        segments.append(seg)
    if len(segments) < 4 or segments[0] != "Volumes":
        raise _Unreadable("the Files API is checked only under /Volumes/<catalog>/<schema>/<volume>")
    if r.method.upper() in ("GET", "HEAD") and r.body:
        raise _Unreadable("a read with a body")
    return {"_resource": "volume:" + ".".join(_canon(s, "uc") for s in segments[1:4])}


# -- the Permissions API ----------------------------------------------------------


def _strings(v: Any) -> list[str]:
    """Every member name and string value in a JSON document."""
    if isinstance(v, str):
        return [v]
    if isinstance(v, dict):
        return [s for k, x in v.items() for s in (k, *_strings(x))]
    if isinstance(v, list):
        return [s for x in v for s in _strings(x)]
    return []


def _object_acl(r: Request, vars_: Mapping[str, str]) -> dict[str, str]:
    """/api/2.0/permissions/{object_type}/{object_id}: CAN_MANAGE on the object.
    Granting IS_OWNER changes the owner, which needs more than CAN_MANAGE: it
    is refused wherever it appears in the body, in any spelling."""
    rtype = _PERMISSION_TYPES.get(vars_.get("object_type", ""))
    if rtype is None:
        raise _Unreadable(f"permissions on {vars_.get('object_type', '')!r} are not checked; toolpass checks {', '.join(sorted(_PERMISSION_TYPES))}")
    oid = _canon(vars_.get("object_id", ""), "ws")
    if r.method.upper() in ("PUT", "PATCH"):
        doc = _body(r)
        if any(_norm(k) == "accesscontrollist" and k != "access_control_list" for k in doc):
            raise _Unreadable("access_control_list is given in another spelling")
        acl = doc.get("access_control_list")
        if acl is not None and not isinstance(acl, list):
            raise _Unreadable("access_control_list is not a list")
        if any("IS_OWNER" in s.upper() or _norm(s) == "isowner" for s in _strings(doc)):
            raise _Unreadable("granting IS_OWNER changes the object's owner; not routed")
    elif r.body:
        raise _Unreadable("a read with a body")
    return {"_resource": f"{rtype}:{oid}"}


# -- the table ----------------------------------------------------------------------


def _routes() -> list[Route]:
    out: list[Route] = []
    agent_list = "a list the workspace filters by the agent's own access"
    run_id = _refuse("a run is named by run_id, which does not say which job it belongs to; read a job's runs with jobs/runs/list?job_id=")
    # Jobs.
    for v in _JOBS:
        j = f"/api/{v}/jobs"
        out += [
            Route("GET", f"{j}/get", "job.view", "job:{id}", body=_query_id("job_id"), spec=False),
            Route("GET", f"{j}/list", None, "", description=agent_list, spec=False),
            Route("GET", f"{j}/runs/list", "job.view", "job:{id}", body=_query_id("job_id"), spec=False),
            Route("POST", f"{j}/run-now", "job.run", "job:{id}", body=_body_id("job_id"), spec=False),
            Route("POST", f"{j}/runs/cancel-all", "job.run", "job:{id}", body=_body_id("job_id", forbid=("all_queued_runs",)), spec=False),
            Route("POST", f"{j}/delete", "job.manage", "job:{id}", effect="destructive", body=_body_id("job_id"), spec=False),
            *(Route("GET", f"{j}/runs/{p}", None, "", body=run_id, spec=False) for p in ("get", "get-output", "export")),
        ]
    # Clusters.
    for v in _CLUSTERS:
        c = f"/api/{v}/clusters"
        out += [
            Route("GET", f"{c}/get", "cluster.attach", "cluster:{id}", body=_query_id("cluster_id", "ws"), spec=False),
            Route("GET", f"{c}/list", None, "", description=agent_list, spec=False),
            Route("POST", f"{c}/events", "cluster.attach", "cluster:{id}", effect="read", body=_body_id("cluster_id", "ws"), spec=False),
            Route("POST", f"{c}/start", "cluster.restart", "cluster:{id}", body=_body_id("cluster_id", "ws"), spec=False),
            Route("POST", f"{c}/restart", "cluster.restart", "cluster:{id}", body=_body_id("cluster_id", "ws"), spec=False),
            Route(
                "POST",
                f"{c}/delete",
                "cluster.restart",
                "cluster:{id}",
                effect="destructive",
                body=_body_id("cluster_id", "ws"),
                description="terminates the cluster",
                spec=False,
            ),
            Route("POST", f"{c}/permanent-delete", "cluster.manage", "cluster:{id}", effect="destructive", body=_body_id("cluster_id", "ws"), spec=False),
            Route("POST", f"{c}/resize", "cluster.manage", "cluster:{id}", body=_body_id("cluster_id", "ws"), spec=False),
            *(
                Route("GET", f"{c}/{p}", None, "", untrusted_output=False, description="the workspace's catalog of runtimes and machines", spec=False)
                for p in ("spark-versions", "list-node-types", "list-zones")
            ),
        ]
    out += [
        Route("GET", "/api/2.0/libraries/cluster-status", "cluster.attach", "cluster:{id}", body=_query_id("cluster_id", "ws"), spec=False),
        # SQL warehouses.
        Route("GET", "/api/2.0/sql/warehouses", None, "", description=agent_list, spec=False),
        Route("GET", "/api/2.0/sql/warehouses/{id}", "raw:CAN_VIEW", "warehouse:{id}", body=_path_id(), spec=False),
        Route("POST", "/api/2.0/sql/warehouses/{id}/start", "warehouse.use", "warehouse:{id}", body=_path_id(), spec=False),
        Route("POST", "/api/2.0/sql/warehouses/{id}/stop", "warehouse.manage", "warehouse:{id}", body=_path_id(), spec=False),
        Route("POST", "/api/2.0/sql/warehouses/{id}/edit", "warehouse.manage", "warehouse:{id}", body=_path_id(), spec=False),
        Route("DELETE", "/api/2.0/sql/warehouses/{id}", "warehouse.manage", "warehouse:{id}", body=_path_id(), spec=False),
        # Pipelines.
        Route("GET", "/api/2.0/pipelines", None, "", description=agent_list, spec=False),
        Route("GET", "/api/2.0/pipelines/{id}", "raw:CAN_VIEW", "pipeline:{id}", body=_path_id(), spec=False),
        Route("GET", "/api/2.0/pipelines/{id}/updates", "raw:CAN_VIEW", "pipeline:{id}", body=_path_id(), spec=False),
        Route("GET", "/api/2.0/pipelines/{id}/updates/{update_id}", "raw:CAN_VIEW", "pipeline:{id}", body=_path_id(), spec=False),
        Route("GET", "/api/2.0/pipelines/{id}/events", "raw:CAN_VIEW", "pipeline:{id}", body=_path_id(), spec=False),
        Route("POST", "/api/2.0/pipelines/{id}/updates", "pipeline.run", "pipeline:{id}", body=_path_id(), spec=False),
        Route("POST", "/api/2.0/pipelines/{id}/stop", "pipeline.run", "pipeline:{id}", body=_path_id(), spec=False),
        Route("DELETE", "/api/2.0/pipelines/{id}", "raw:CAN_MANAGE", "pipeline:{id}", body=_path_id(), spec=False),
        # Git folders.
        Route("GET", "/api/2.0/repos", None, "", description=agent_list, spec=False),
        Route("GET", "/api/2.0/repos/{id}", "notebook.read", "repo:{id}", body=_path_id("num"), spec=False),
        Route("PATCH", "/api/2.0/repos/{id}", "notebook.edit", "repo:{id}", body=_path_id("num"), description="check out a branch or tag", spec=False),
        Route("DELETE", "/api/2.0/repos/{id}", "raw:CAN_MANAGE", "repo:{id}", body=_path_id("num"), spec=False),
        # Model serving: the endpoint API names an endpoint by name, its ACL by id; reads by name are refused
        # (an unrouted read would pass unchecked), writes by name have no route (see unrouted below).
        Route("GET", "/api/2.0/serving-endpoints", None, "", description=agent_list, spec=False),
        Route("GET", "/api/2.0/serving-endpoints/{name}", None, "", body=_refuse(_REASON_ENDPOINT_NAME), spec=False),
        Route("GET", "/api/2.0/serving-endpoints/{name}/{rest+}", None, "", body=_refuse(_REASON_ENDPOINT_NAME), spec=False),
        # Access control lists on workspace objects.
        *(
            Route(m, "/api/2.0/permissions/{object_type}/{object_id}", "raw:CAN_MANAGE", "", body=_guard(_object_acl), spec=False)
            for m in ("GET", "PUT", "PATCH")
        ),
        Route(
            "GET",
            "/api/2.0/permissions/{object_type}/{object_id}/permissionLevels",
            None,
            "",
            untrusted_output=False,
            description="the names of the levels an object type has",
            spec=False,
        ),
        # Files in Unity Catalog volumes. A ** tail, not {path+}, so a path with an empty segment
        # still matches and is refused, rather than passing as a read toolpass has no route for.
        *(
            Route(m, f"/api/2.0/fs/{kind}/**", action, "", body=_guard(_volume_file), spec=False)
            for kind in ("files", "directories")
            for m, action in (("GET", "volume.read"), ("HEAD", "volume.read"), ("PUT", "volume.write"), ("DELETE", "volume.write"))
        ),
        # The Workspace and DBFS APIs name objects by path; listings are filtered by the agent's own access.
        Route("GET", "/api/2.0/workspace/list", None, "", description=agent_list, spec=False),
        Route("GET", "/api/2.0/workspace/get-status", None, "", description="an object's type and id, if the agent can see it", spec=False),
        Route(
            "GET",
            "/api/2.0/workspace/export",
            None,
            "",
            body=_refuse("a notebook's content is checked by notebook id; the Workspace API names it by path"),
            spec=False,
        ),
        Route("GET", "/api/2.0/dbfs/read", None, "", body=_refuse("DBFS files have no permissions toolpass checks"), spec=False),
        Route("GET", "/api/2.0/secrets/{rest+}", None, "", body=_refuse("secret scopes have permissions toolpass does not check"), spec=False),
        # Statements and commands run as the agent; their results are refused with them.
        Route("GET", "/api/2.0/sql/statements/{rest+}", None, "", body=_refuse("SQL statement execution is not routed"), spec=False),
        Route("GET", "/api/1.2/{rest+}", None, "", body=_refuse("the command execution API runs code as the agent; not routed"), spec=False),
        Route("GET", "/api/2.0/preview/scim/v2/Me", None, "", untrusted_output=False, description="the agent's own identity", spec=False),
    ]
    # Unity Catalog.
    # Reads need the parents' USE_CATALOG and USE_SCHEMA as well, as Databricks enforces them.
    uc_reads = {
        "catalog": "catalog.use",
        "schema": "schema.read",
        "table": "table.read",
        "volume": "volume.read",
        "function": "function.execute",
        "model": "model.read",
    }
    for rtype, read in uc_reads.items():
        coll = f"{_UC}/{UC_TYPES[rtype].securable}s"
        item = coll + "/{full_name}"
        res = f"{rtype}:{{name}}"
        manage = _uc_manage(rtype)
        out += [
            Route("GET", coll, None, "", description=agent_list, spec=False),
            Route("GET", item, read, res, body=_uc_path(rtype), spec=False),
            Route("PATCH", item, manage, res, body=_uc_path(rtype), description="rename, change the owner or properties", spec=False),
            Route("DELETE", item, manage, res, body=_uc_path(rtype), spec=False),
        ]
    out += [
        Route("GET", f"{_UC}/tables/{{full_name}}/exists", "table.read", "table:{name}", body=_uc_path("table"), spec=False),
        Route("GET", f"{_UC}/models/{{full_name}}/versions", "model.read", "model:{name}", body=_uc_path("model"), spec=False),
        Route("GET", f"{_UC}/models/{{full_name}}/versions/{{version}}", "model.read", "model:{name}", body=_uc_path("model"), spec=False),
        Route("PATCH", f"{_UC}/models/{{full_name}}/versions/{{version}}", "model.manage", "model:{name}", body=_uc_path("model"), spec=False),
        Route("DELETE", f"{_UC}/models/{{full_name}}/versions/{{version}}", "model.manage", "model:{name}", body=_uc_path("model"), spec=False),
        Route("PUT", f"{_UC}/models/{{full_name}}/aliases/{{alias}}", "model.manage", "model:{name}", body=_uc_path("model"), spec=False),
        Route("DELETE", f"{_UC}/models/{{full_name}}/aliases/{{alias}}", "model.manage", "model:{name}", body=_uc_path("model"), spec=False),
        # Grants: reading them unfiltered, and changing them, take MANAGE (the action follows the securable type).
        *(
            Route(m, f"{_UC}/{p}/{{securable_type}}/{{full_name}}", "uc.manage", "", body=_guard(_uc_grants), effect=e, spec=False)
            for p in ("permissions", "effective-permissions")
            for m, e in (("GET", "read"), ("PATCH", "write"))
            if not (p == "effective-permissions" and m == "PATCH")
        ),
        # New schemas, volumes and models: the privilege on the parent.
        Route("POST", f"{_UC}/schemas", "schema.create", "", body=_uc_create("catalog", "catalog_name", forbid=("storage_root",)), spec=False),
        Route(
            "POST",
            f"{_UC}/volumes",
            "volume.create",
            "",
            body=_uc_create("schema", "catalog_name", "schema_name", forbid=("storage_location",)),
            spec=False,
        ),
        Route(
            "POST",
            f"{_UC}/models",
            "model.create",
            "",
            body=_uc_create("schema", "catalog_name", "schema_name", forbid=("storage_location",)),
            spec=False,
        ),
    ]
    return out


_REASON_ENDPOINT_NAME = (
    "the serving endpoints API names an endpoint by name, while its permissions are kept under the endpoint's id, which the request does not carry; not routed"
)
_REASON_RUNS_AS_AGENT = "runs code or SQL as the agent's principal, not as the user"
_REASON_RUN_ID = "a run is named by run_id, which does not say which job it belongs to"
_REASON_CALLER = "Databricks checks these settings (run-as identity, cluster policy, pool, access mode) against the agent's principal"
_REASON_PATH = "the Workspace API names objects by path; toolpass checks notebooks and directories by id"

ROUTES = RouteTable(
    "databricks",
    _routes(),
    claims=("/api",),
    unrouted={
        ("POST", "/api/2.0/sql/statements"): _REASON_RUNS_AS_AGENT + "; toolpass does not read Databricks SQL",
        ("POST", "/api/2.0/sql/statements/{statement_id}/cancel"): "statement execution is not routed",
        ("*", "/api/1.2/**"): _REASON_RUNS_AS_AGENT,
        **{("POST", f"/api/{v}/jobs/runs/submit"): _REASON_RUNS_AS_AGENT for v in _JOBS},
        **{("POST", f"/api/{v}/jobs/runs/{p}"): _REASON_RUN_ID for v in _JOBS for p in ("cancel", "delete", "repair")},
        **{("POST", f"/api/{v}/jobs/{p}"): _REASON_CALLER for v in _JOBS for p in ("update", "reset")},
        **{("POST", f"/api/{v}/jobs/create"): "a new job belongs to the agent and runs as it" for v in _JOBS},
        **{("POST", f"/api/{v}/clusters/{p}"): _REASON_CALLER for v in _CLUSTERS for p in ("edit", "update")},
        **{("POST", f"/api/{v}/clusters/create"): "a new cluster belongs to the agent" for v in _CLUSTERS},
        **{("POST", f"/api/{v}/clusters/{p}"): "workspace admins only" for v in _CLUSTERS for p in ("pin", "unpin", "change-owner")},
        ("PUT", "/api/2.0/pipelines/{pipeline_id}"): _REASON_CALLER,
        ("POST", "/api/2.0/pipelines"): "a new pipeline belongs to the agent and runs as it",
        ("POST", "/api/2.0/sql/warehouses"): "a new warehouse belongs to the agent",
        ("POST", "/api/2.0/repos"): "a new Git folder is placed by workspace path, which toolpass does not check",
        ("POST", "/api/2.0/serving-endpoints"): "a new endpoint belongs to the agent",
        ("POST", "/serving-endpoints/{name}/invocations"): _REASON_ENDPOINT_NAME,
        ("DELETE", "/api/2.0/serving-endpoints/{name}"): _REASON_ENDPOINT_NAME,
        ("*", "/api/2.0/serving-endpoints/{name}/**"): _REASON_ENDPOINT_NAME,
        ("POST", "/api/2.0/libraries/{rest+}"): "installs code from paths Databricks checks against the agent's principal",
        **{("POST", f"/api/2.0/workspace/{p}"): _REASON_PATH for p in ("import", "mkdirs", "delete")},
        ("POST", "/api/2.0/dbfs/{rest+}"): "DBFS files have no permissions toolpass checks",
        ("*", "/api/2.0/secrets/**"): "secret scopes have permissions toolpass does not check",
        ("POST", "/api/2.0/token/create"): "mints a token for the agent's principal",
        ("POST", f"{_UC}/catalogs"): "needs CREATE_CATALOG on the metastore, which toolpass does not check",
        ("POST", f"{_UC}/tables"): "a new table or view belongs to the agent, and a view reads with its owner's rights",
        ("POST", f"{_UC}/functions"): "a new function belongs to the agent and reads with its owner's rights",
        ("*", f"{_UC}/**"): "Unity Catalog administration (locations, credentials, shares, metastores) is not routed",
    },
)

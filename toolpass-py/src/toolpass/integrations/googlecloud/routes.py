"""Google Cloud REST requests, mapped to the IAM permission each needs on the
resource it touches, which the Policy Troubleshooter then answers for the
user.

The services the googlecloud integration names resources for are routed:
Cloud Storage (JSON API), BigQuery, Secret Manager, IAM service accounts
and roles, IAM Credentials, Compute Engine instances, Cloud Run services,
GKE clusters and Resource Manager projects, folders and organizations. Each
request checks one permission (``storage.objects.get``,
``secretmanager.versions.access``) on the resource in its path, through the
integration's named action when one asks exactly that permission
(``storage.read``) and ``raw:<permission>`` otherwise; ``setIamPolicy`` is
``iam.set`` where the integration knows the type, and sends out (it shares).

Requests that need a second permission or a second resource are refused:
an upload that may overwrite an object (without ``ifGenerationMatch=0``),
copies, rewrites and composes, BigQuery jobs and queries (SQL names its own
tables), table schemas that set or may drop column-level security
(``bigquery.tables.setCategory``), Cloud Run deploys and Compute changes that run code as a service
account (``iam.serviceAccounts.actAs``), token minting through a chain of
``delegates``. Every read toolpass does not know is refused, not passed:
the hosts are Google Cloud's own. The XML API, virtual-hosted buckets
(``<bucket>.storage.googleapis.com``), regional endpoints and other
services are not claimed.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Mapping
from typing import Any

from toolpass.core.catalog import ResourceError, parse_resource
from toolpass.core.decision import ToolpassError
from toolpass.core.routes import Extractor, Request, Route, RouteTable
from toolpass.integrations._googleapis import Unreadable, body_object, guarded, param, refusing, upload_metadata
from toolpass.integrations.googlecloud.actions import (
    ACTION_LIST,
    HIERARCHY_TYPES,
    SET_IAM_POLICY_PERMISSIONS,
    full_resource_name,
    is_object_name,
    is_project,
    parse_ref,
)

__all__ = ["HOSTS", "ROUTES"]

_GCS = "storage.googleapis.com"
_BQ = "bigquery.googleapis.com"
_SM = "secretmanager.googleapis.com"
_IAM = "iam.googleapis.com"
_IAMC = "iamcredentials.googleapis.com"
_GCE = "compute.googleapis.com"
_RUN = "run.googleapis.com"
_GKE = "container.googleapis.com"
_CRM = "cloudresourcemanager.googleapis.com"
_PT = "policytroubleshooter.googleapis.com"

HOSTS = (_GCS, "storage.cloud.google.com", _BQ, _SM, _IAM, _IAMC, _GCE, _RUN, _GKE, _CRM, _PT)
# The same services under www.googleapis.com and content.googleapis.com
# (their JSON paths, uploads, media downloads and batches): claimed so a
# request there is refused rather than passed unseen.
_ALIASES = (
    "/storage/v1",
    "/upload/storage/v1",
    "/resumable/upload/storage/v1",
    "/download/storage/v1",
    "/batch/storage/v1",
    "/bigquery/v2",
    "/upload/bigquery/v2",
    "/batch/bigquery/v2",
    "/compute/v1",
    "/batch/compute/v1",
)
_SHARED = ("www.googleapis.com", "content.googleapis.com")

_NAMED = {a.permission: a for a in ACTION_LIST if a.permission}

Build = Callable[[Request, Mapping[str, str]], Mapping[str, str]]


# -- resources and actions --------------------------------------------------------------


def _action(permission: str, resource: str) -> str:
    """The named action that asks exactly ``permission`` of this resource
    type, or raw:<permission>."""
    typ = resource.partition(":")[0]
    if SET_IAM_POLICY_PERMISSIONS.get(typ) == permission:
        return "iam.set"
    a = _NAMED.get(permission)
    if a is not None and (typ in a.types or typ in HIERARCHY_TYPES):
        return a.name
    return "raw:" + permission


def _check(permission: str, resource: str, effect: str | None = None) -> dict[str, str]:
    """``permission`` on ``resource``, once the integration's own parser
    accepts both: a resource it would reject is refused here."""
    action = _action(permission, resource)
    try:
        parse_ref(action, parse_resource(resource))
    except (ToolpassError, ResourceError) as e:
        raise Unreadable(f"{resource} is not a resource the googlecloud check reads ({e})") from None
    out = {"_action": action, "_resource": resource}
    if effect is not None:
        out["_effect"] = effect
    return out


def _unchecked(effect: str = "read") -> dict[str, str]:
    """No check: the caller's own permissions, nothing user-scoped."""
    return {"_action": "", "_resource": "", "_effect": effect}


def _project(p: str) -> str:
    if not is_project(p):
        raise Unreadable(f"{p!r} is not a project id or number")
    return f"project:{p}"


def _proj(v: Mapping[str, str]) -> str:
    return _project(v["project"])


def _verb(segment: str) -> tuple[str, str]:
    """``name:verb`` (Google's custom methods) as (name, verb); verb "" when there is none."""
    name, _, verb = segment.partition(":")
    if not name:
        raise Unreadable(f"{segment!r} does not name a resource")
    return name, verb


def _canon_keys(meta: Mapping[str, Any]) -> set[str]:
    """Field names as Google's JSON reader matches them (camelCase and
    snake_case alike); a field given both ways is refused."""
    out: set[str] = set()
    for k in meta:
        c = k.replace("_", "").lower()
        if c in out:
            raise Unreadable(f"the body gives the field {k!r} more than once")
        out.add(c)
    return out


def _on(host: str, build: Build) -> Extractor:
    """An extractor for one service: the request must be addressed to its
    host, since the same path means something else, or nothing, on another."""

    def run(r: Request, v: Mapping[str, str]) -> Mapping[str, str]:
        if r.host.lower().rstrip(".") != host:
            raise Unreadable(f"{r.host} does not serve this path; it belongs to {host}")
        return build(r, v)

    return guarded(run)


def _fixed(permission: str, resource: Callable[[Mapping[str, str]], str], effect: str | None = None) -> Build:
    return lambda r, v: _check(permission, resource(v), effect)


def _iam_verbs(prefix: str, resource: Callable[[Mapping[str, str]], str], name_var: str, extra: Mapping[str, tuple[str, str | None]] | None = None) -> Build:
    """POST <name>:getIamPolicy, :setIamPolicy, :testIamPermissions, and any
    ``extra`` verbs (verb -> (permission, effect)); other verbs are refused."""
    verbs: dict[str, tuple[str, str | None]] = {
        "getIamPolicy": (prefix + ".getIamPolicy", "read"),
        "setIamPolicy": (prefix + ".setIamPolicy", None),
        **(extra or {}),
    }

    def build(r: Request, v: Mapping[str, str]) -> Mapping[str, str]:
        _, verb = _verb(v[name_var])
        if verb == "testIamPermissions":
            return _unchecked()
        if verb not in verbs:
            raise Unreadable(f"the method :{verb or '(none)'} is not routed")
        permission, effect = verbs[verb]
        return _check(permission, resource(v), effect)

    return build


# -- Cloud Storage ----------------------------------------------------------------------

_B = "/storage/v1/b"
_BK = _B + "/{bucket}"
_OBJ = _BK + "/o/{object}"
_ACL_FIELDS = frozenset({"acl", "defaultobjectacl"})
# Fields Google fills in and ignores on a write (a PUT sends the resource back whole).
_OUTPUT_FIELDS = frozenset(
    {"kind", "id", "selflink", "name", "bucket", "etag", "generation", "metageneration", "timecreated", "updated", "projectnumber", "owner"}
    | {"location", "locationtype", "size", "md5hash", "crc32c", "medialink", "componentcount", "timedeleted", "timestorageclassupdated"}
)
# What storage.buckets.update and storage.objects.update cover. Other fields
# need a permission of their own (setRetention, setIpFilter) or name a second
# resource, and are refused.
_BUCKET_FIELDS = frozenset(
    {"labels", "lifecycle", "cors", "website", "versioning", "logging", "defaulteventbasedhold", "retentionpolicy", "billing", "encryption"}
    | {"iamconfiguration", "autoclass", "softdeletepolicy", "rpo", "storageclass"}
)
_OBJECT_FIELDS = frozenset(
    {"contenttype", "contentencoding", "contentdisposition", "contentlanguage", "cachecontrol", "metadata", "temporaryhold", "eventbasedhold", "customtime"}
)


def _bucket(v: Mapping[str, str]) -> str:
    return f"bucket:{v['bucket']}"


def _object(v: Mapping[str, str]) -> str:
    return _object_named(v["bucket"], v["object"])


def _object_named(bucket: str, name: str) -> str:
    if "?" in name:
        # A resource reference ends its id at "?"; escaping it would ask the troubleshooter about another object's name.
        raise Unreadable(f"{name!r}: an object name with ? cannot be named as a toolpass resource; not routed")
    if not is_object_name(name):
        raise Unreadable(f"{name!r} is not an object name toolpass can check (no control characters, no . or .. segments)")
    return f"object:{bucket}/{name}"


def _project_param(r: Request, v: Mapping[str, str]) -> str:
    p = param(r, "project")
    if not p:
        raise Unreadable("the request does not name the project (project=)")
    return _project(p)


def _metadata_update(r: Request, kind: str, fields: frozenset[str], predefined: tuple[str, ...], resource: str) -> Mapping[str, str]:
    """A bucket's or an object's metadata update: setIamPolicy when it
    changes ACLs and nothing else, update when it changes only fields that
    permission covers, refused otherwise."""
    keys = _canon_keys(body_object(r)) - _OUTPUT_FIELDS
    if keys & _ACL_FIELDS or any(param(r, p) for p in predefined):
        if keys - _ACL_FIELDS:
            raise Unreadable("change ACLs on their own, without other metadata")
        return _check(f"storage.{kind}.setIamPolicy", resource)
    other = sorted(keys - fields)
    if other:
        raise Unreadable(f"{', '.join(other)}: more than storage.{kind}.update covers; not routed")
    return _check(f"storage.{kind}.update", resource)


def _bucket_update(r: Request, v: Mapping[str, str]) -> Mapping[str, str]:
    return _metadata_update(r, "buckets", _BUCKET_FIELDS, ("predefinedAcl", "predefinedDefaultObjectAcl"), _bucket(v))


def _object_update(r: Request, v: Mapping[str, str]) -> Mapping[str, str]:
    return _metadata_update(r, "objects", _OBJECT_FIELDS, ("predefinedAcl",), _object(v))


def _upload(r: Request, v: Mapping[str, str]) -> Mapping[str, str]:
    """objects.insert: the object named by ``name`` (query) or the metadata.
    Writing over an existing object also needs storage.objects.delete, so
    only an upload that cannot (ifGenerationMatch=0) is routed."""
    _, meta = upload_metadata(r)
    keys = _canon_keys(meta)
    named = param(r, "name")
    inner = meta.get("name")
    if inner is not None and not isinstance(inner, str):
        raise Unreadable("the metadata's name is not a string")
    if named is not None and inner is not None and named != inner:
        raise Unreadable("the query and the metadata name different objects")
    name = named if named is not None else inner
    if not name:
        raise Unreadable("the upload does not name the object (name)")
    if "acl" in keys or param(r, "predefinedAcl"):
        raise Unreadable("an ACL set at upload also needs storage.objects.setIamPolicy; set it separately")
    if param(r, "ifGenerationMatch") != "0":
        raise Unreadable("an upload may replace an existing object, which also needs storage.objects.delete; add ifGenerationMatch=0 so it only creates")
    return _check("storage.objects.create", _object_named(v["bucket"], name))


def _upload_chunk(r: Request, v: Mapping[str, str]) -> Mapping[str, str]:
    """A resumable upload's next chunk: the object was checked when the
    session was opened; the bucket is checked again."""
    if param(r, "upload_id") is None:
        raise Unreadable("a PUT to an upload path continues a resumable upload and needs its upload_id")
    return _check("storage.objects.create", _bucket(v))


def _storage() -> list[Route]:
    def on(build: Build) -> Extractor:
        return _on(_GCS, build)

    acl_read_b = on(_fixed("storage.buckets.getIamPolicy", _bucket))
    acl_write_b = on(_fixed("storage.buckets.setIamPolicy", _bucket))
    acl_read_o = on(_fixed("storage.objects.getIamPolicy", _object))
    acl_write_o = on(_fixed("storage.objects.setIamPolicy", _object))
    out = [
        Route("GET", _B, "raw:storage.buckets.list", "project:{project}", body=on(lambda r, v: _check("storage.buckets.list", _project_param(r, v)))),
        Route("POST", _B, "raw:storage.buckets.create", "project:{project}", body=on(lambda r, v: _check("storage.buckets.create", _project_param(r, v)))),
        Route("GET", _BK, "raw:storage.buckets.get", "bucket:{bucket}", body=on(_fixed("storage.buckets.get", _bucket))),
        Route("PATCH", _BK, "raw:storage.buckets.update", "bucket:{bucket}", body=on(_bucket_update)),
        Route("PUT", _BK, "raw:storage.buckets.update", "bucket:{bucket}", body=on(_bucket_update)),
        Route("DELETE", _BK, "bucket.delete", "bucket:{bucket}", body=on(_fixed("storage.buckets.delete", _bucket))),
        Route("GET", _BK + "/iam", "raw:storage.buckets.getIamPolicy", "bucket:{bucket}", body=acl_read_b),
        Route("PUT", _BK + "/iam", "iam.set", "bucket:{bucket}", sends_out=True, body=acl_write_b),
        Route(
            "GET", _BK + "/iam/testPermissions", None, "", untrusted_output=False, body=on(lambda r, v: _unchecked()), description="the agent's own permissions"
        ),
        Route("GET", _BK + "/o", "storage.list", "bucket:{bucket}", body=on(_fixed("storage.objects.list", _bucket))),
        Route("POST", _BK + "/o/watch", "storage.list", "bucket:{bucket}", sends_out=True, body=on(_fixed("storage.objects.list", _bucket))),
        Route("GET", _OBJ, "storage.read", "object:{bucket}/{object}", body=on(_fixed("storage.objects.get", _object))),
        # The media download service (the client's downloads, an object's mediaLink): the same read.
        Route("GET", "/download" + _OBJ, "storage.read", "object:{bucket}/{object}", body=on(_fixed("storage.objects.get", _object))),
        Route("PATCH", _OBJ, "raw:storage.objects.update", "object:{bucket}/{object}", body=on(_object_update)),
        Route("PUT", _OBJ, "raw:storage.objects.update", "object:{bucket}/{object}", body=on(_object_update)),
        Route("DELETE", _OBJ, "storage.delete", "object:{bucket}/{object}", body=on(_fixed("storage.objects.delete", _object))),
        Route(
            "POST",
            "/batch/storage/v1",
            None,
            "",
            body=refusing("a batch request carries several requests toolpass cannot check one by one; send them separately"),
        ),
    ]
    for up in ("/upload", "/resumable/upload"):
        out += [
            Route("POST", up + _BK + "/o", "storage.write", "object:{bucket}/{name}", body=on(_upload)),
            Route("PUT", up + _BK + "/o", "storage.write", "bucket:{bucket}", body=on(_upload_chunk)),
        ]
    # ACLs are IAM in Cloud Storage's model: reading them is getIamPolicy, changing them setIamPolicy.
    for coll in ("/acl", "/defaultObjectAcl"):
        out += [
            Route("GET", _BK + coll, "raw:storage.buckets.getIamPolicy", "bucket:{bucket}", body=acl_read_b),
            Route("POST", _BK + coll, "iam.set", "bucket:{bucket}", sends_out=True, body=acl_write_b),
            Route("GET", _BK + coll + "/{entity}", "raw:storage.buckets.getIamPolicy", "bucket:{bucket}", body=acl_read_b),
            Route("PUT", _BK + coll + "/{entity}", "iam.set", "bucket:{bucket}", sends_out=True, body=acl_write_b),
            Route("PATCH", _BK + coll + "/{entity}", "iam.set", "bucket:{bucket}", sends_out=True, body=acl_write_b),
            Route("DELETE", _BK + coll + "/{entity}", "iam.set", "bucket:{bucket}", body=acl_write_b),
        ]
    out += [
        Route("GET", _OBJ + "/acl", "raw:storage.objects.getIamPolicy", "object:{bucket}/{object}", body=acl_read_o),
        Route("POST", _OBJ + "/acl", "raw:storage.objects.setIamPolicy", "object:{bucket}/{object}", sends_out=True, body=acl_write_o),
        Route("GET", _OBJ + "/acl/{entity}", "raw:storage.objects.getIamPolicy", "object:{bucket}/{object}", body=acl_read_o),
        Route("PUT", _OBJ + "/acl/{entity}", "raw:storage.objects.setIamPolicy", "object:{bucket}/{object}", sends_out=True, body=acl_write_o),
        Route("PATCH", _OBJ + "/acl/{entity}", "raw:storage.objects.setIamPolicy", "object:{bucket}/{object}", sends_out=True, body=acl_write_o),
        Route("DELETE", _OBJ + "/acl/{entity}", "raw:storage.objects.setIamPolicy", "object:{bucket}/{object}", body=acl_write_o),
    ]
    return out


# -- BigQuery ---------------------------------------------------------------------------

_BQP = "/bigquery/v2/projects/{projectId}"
_DS = _BQP + "/datasets/{datasetId}"
_TB = _DS + "/tables/{tableId}"
# Fields BigQuery fills in and ignores on a write.
_BQ_OUTPUT = frozenset(
    {"kind", "id", "selflink", "etag", "creationtime", "lastmodifiedtime", "location", "type", "numbytes", "numrows", "numlongtermbytes"}
    | {"streamingbuffer", "numphysicalbytes", "numtimetravelphysicalbytes", "numtotallogicalbytes", "numactivelogicalbytes", "numlongtermlogicalbytes"}
)
# What datasets.update covers. access shares the dataset; a linked or an
# external dataset reads another source with rights of its own.
_DATASET_FIELDS = frozenset(
    {"description", "friendlyname", "labels", "defaulttableexpirationms", "defaultpartitionexpirationms", "defaultcollation", "maxtimetravelhours"}
    | {"storagebillingmodel", "iscaseinsensitive", "defaultroundingmode", "defaultencryptionconfiguration", "datasetreference"}
)
# What tables.create and tables.update cover. A view, a materialized view,
# an external, cloned or snapshot table reads other data with rights of its
# own, and is refused.
_TABLE_FIELDS = frozenset(
    {"description", "friendlyname", "labels", "expirationtime", "schema", "timepartitioning", "rangepartitioning", "clustering"}
    | {"requirepartitionfilter", "encryptionconfiguration", "defaultcollation", "defaultroundingmode", "tablereference", "tableconstraints"}
)


def _dataset(v: Mapping[str, str]) -> str:
    return f"dataset:{v['projectId']}/{v['datasetId']}"


def _table(v: Mapping[str, str]) -> str:
    name, _ = _verb(v["tableId"])
    return f"table:{v['projectId']}/{v['datasetId']}/{name}"


def _plain_table(v: Mapping[str, str]) -> str:
    if ":" in v["tableId"]:
        raise Unreadable(f"{v['tableId']!r} is not a table id")
    return _table(v)


def _same_place(ref: Any, v: Mapping[str, str], keys: tuple[str, ...]) -> None:
    """A body's datasetReference or tableReference names the path's own place."""
    if ref is None:
        return
    if not isinstance(ref, dict):
        raise Unreadable("the body's reference is not an object")
    for k, var in zip(("projectId", "datasetId", "tableId"), keys, strict=False):
        if k in ref and ref[k] != v[var].partition(":")[0]:
            raise Unreadable(f"the body's {k} names another place than the path")
    if set(ref) - {"projectId", "datasetId", "tableId"}:
        raise Unreadable("the body's reference carries unknown fields")


# Column-level security in a table's schema: setting a column's policy tags
# or data policies needs bigquery.tables.setCategory (and the policies' own
# permissions), more than tables.create or tables.update.
_COLUMN_SECURITY = frozenset({"policytags", "datapolicies"})


def _column_security(node: Any) -> bool:
    """True when ``policyTags`` or ``dataPolicies`` appears anywhere in a schema, in any spelling."""
    if isinstance(node, dict):
        return bool(_canon_keys(node) & _COLUMN_SECURITY) or any(_column_security(x) for x in node.values())
    if isinstance(node, list):
        return any(_column_security(x) for x in node)
    return False


def _definition(permission: str, resource: Callable[[Mapping[str, str]], str], fields: frozenset[str], ref: str, keys: tuple[str, ...]) -> Build:
    """A dataset or table created or changed: only the fields the permission covers, in the path's own place.

    A table's schema carries its columns' policy tags and data policies.
    Setting them needs bigquery.tables.setCategory, so a schema that names
    them is refused. A table update that sends a schema replaces every
    column's, and a column sent without its policy tags loses them, which
    needs the same permission: a schema change on an existing table is
    refused."""

    def build(r: Request, v: Mapping[str, str]) -> Mapping[str, str]:
        body = body_object(r)
        other = sorted(_canon_keys(body) - _BQ_OUTPUT - fields)
        if other:
            raise Unreadable(f"{', '.join(other)}: more than {permission} covers (a view, an external source, an access list); not routed")
        schema = [x for k, x in body.items() if k.replace("_", "").lower() == "schema"]
        if schema and permission == "bigquery.tables.update":
            raise Unreadable(
                "a schema update replaces the columns' policy tags and data policies (column-level security), "
                "which needs bigquery.tables.setCategory; not routed"
            )
        if schema and _column_security(schema[0]):
            raise Unreadable("policyTags and dataPolicies set column-level security, which needs bigquery.tables.setCategory; not routed")
        refs = [v for k, v in body.items() if k.replace("_", "").lower() == ref]
        _same_place(refs[0] if refs else None, v, keys)
        return _check(permission, resource(v))

    return build


def _dataset_delete(r: Request, v: Mapping[str, str]) -> Mapping[str, str]:
    if (param(r, "deleteContents") or "false").lower() != "false":
        raise Unreadable("deleteContents deletes every table in the dataset, each a resource of its own; delete the tables first")
    return _check("bigquery.datasets.delete", _dataset(v))


def _insert_all(r: Request, v: Mapping[str, str]) -> Mapping[str, str]:
    if "templatesuffix" in _canon_keys(body_object(r)):
        raise Unreadable("templateSuffix writes into another table, created on the fly; not routed")
    return _check("bigquery.tables.updateData", _plain_table(v))


def _bigquery() -> list[Route]:
    def on(build: Build) -> Extractor:
        return _on(_BQ, build)

    jobs = refusing("BigQuery jobs and queries name their tables in SQL or move data between tables; toolpass cannot check them table by table")
    return [
        Route("GET", _BQP + "/datasets", None, "", body=on(lambda r, v: _unchecked()), description="the datasets the agent's own credential sees"),
        Route(
            "POST",
            _BQP + "/datasets",
            "raw:bigquery.datasets.create",
            "project:{projectId}",
            body=on(_definition("bigquery.datasets.create", lambda v: _project(v["projectId"]), _DATASET_FIELDS, "datasetreference", ("projectId",))),
        ),
        Route("GET", _DS, "raw:bigquery.datasets.get", "dataset:{projectId}/{datasetId}", body=on(_fixed("bigquery.datasets.get", _dataset))),
        Route(
            "PATCH",
            _DS,
            "raw:bigquery.datasets.update",
            "dataset:{projectId}/{datasetId}",
            body=on(_definition("bigquery.datasets.update", _dataset, _DATASET_FIELDS, "datasetreference", ("projectId", "datasetId"))),
        ),
        Route(
            "PUT",
            _DS,
            "raw:bigquery.datasets.update",
            "dataset:{projectId}/{datasetId}",
            body=on(_definition("bigquery.datasets.update", _dataset, _DATASET_FIELDS, "datasetreference", ("projectId", "datasetId"))),
        ),
        Route("DELETE", _DS, "raw:bigquery.datasets.delete", "dataset:{projectId}/{datasetId}", body=on(_dataset_delete)),
        Route("GET", _DS + "/tables", "raw:bigquery.tables.list", "dataset:{projectId}/{datasetId}", body=on(_fixed("bigquery.tables.list", _dataset))),
        Route(
            "POST",
            _DS + "/tables",
            "raw:bigquery.tables.create",
            "dataset:{projectId}/{datasetId}",
            body=on(_definition("bigquery.tables.create", _dataset, _TABLE_FIELDS, "tablereference", ("projectId", "datasetId"))),
        ),
        Route("GET", _TB, "raw:bigquery.tables.get", "table:{projectId}/{datasetId}/{tableId}", body=on(_fixed("bigquery.tables.get", _plain_table))),
        Route(
            "PATCH",
            _TB,
            "raw:bigquery.tables.update",
            "table:{projectId}/{datasetId}/{tableId}",
            body=on(_definition("bigquery.tables.update", _plain_table, _TABLE_FIELDS, "tablereference", ("projectId", "datasetId", "tableId"))),
        ),
        Route(
            "PUT",
            _TB,
            "raw:bigquery.tables.update",
            "table:{projectId}/{datasetId}/{tableId}",
            body=on(_definition("bigquery.tables.update", _plain_table, _TABLE_FIELDS, "tablereference", ("projectId", "datasetId", "tableId"))),
        ),
        Route("DELETE", _TB, "bigquery.delete", "table:{projectId}/{datasetId}/{tableId}", body=on(_fixed("bigquery.tables.delete", _plain_table))),
        Route(
            "POST",
            _TB,
            "iam.set",
            "table:{projectId}/{datasetId}/{tableId}",
            sends_out=True,
            body=on(_iam_verbs("bigquery.tables", _table, "tableId")),
            description=":getIamPolicy, :setIamPolicy, :testIamPermissions",
        ),
        Route("GET", _TB + "/data", "bigquery.read", "table:{projectId}/{datasetId}/{tableId}", body=on(_fixed("bigquery.tables.getData", _plain_table))),
        Route("POST", _TB + "/insertAll", "bigquery.write", "table:{projectId}/{datasetId}/{tableId}", body=on(_insert_all)),
        *(Route(m, _BQP + p, None, "", body=jobs) for m in ("GET", "POST") for p in ("/jobs", "/queries")),
        *(Route(m, _BQP + p + "/{rest+}", None, "", body=jobs) for m in ("GET", "POST", "DELETE") for p in ("/jobs", "/queries")),
    ]


# -- Secret Manager -----------------------------------------------------------------------

_SEC = "/v1/projects/{project}/secrets"


def _secret(v: Mapping[str, str]) -> str:
    name, _ = _verb(v["secret"])
    return f"secret:{v['project']}/{name}"


def _secret_get(r: Request, v: Mapping[str, str]) -> Mapping[str, str]:
    _, verb = _verb(v["secret"])
    if verb == "getIamPolicy":
        return _check("secretmanager.secrets.getIamPolicy", _secret(v))
    if verb:
        raise Unreadable(f"the method :{verb} is not routed")
    return _check("secretmanager.secrets.get", _secret(v))


def _plain_secret(permission: str) -> Build:
    def build(r: Request, v: Mapping[str, str]) -> Mapping[str, str]:
        if ":" in v["secret"]:
            raise Unreadable(f"{v['secret']!r} is not a secret id")
        return _check(permission, _secret(v))

    return build


def _version(method: str) -> Build:
    """A secret version: GET <v> or <v>:access, POST <v>:enable, :disable, :destroy."""
    verbs = (
        {"": ("secretmanager.versions.get", None), "access": ("secretmanager.versions.access", None)}
        if method == "GET"
        else {
            "enable": ("secretmanager.versions.enable", None),
            "disable": ("secretmanager.versions.disable", None),
            "destroy": ("secretmanager.versions.destroy", "destructive"),
        }
    )

    def build(r: Request, v: Mapping[str, str]) -> Mapping[str, str]:
        _, verb = _verb(v["version"])
        if verb not in verbs or ":" in v["secret"]:
            raise Unreadable(f"the method :{verb or '(none)'} is not routed on a secret version")
        permission, effect = verbs[verb]
        return _check(permission, _secret(v), effect)

    return build


def _secret_manager() -> list[Route]:
    def on(build: Build) -> Extractor:
        return _on(_SM, build)

    res = "secret:{project}/{secret}"
    return [
        Route("GET", _SEC, "raw:secretmanager.secrets.list", "project:{project}", body=on(_fixed("secretmanager.secrets.list", _proj))),
        Route("POST", _SEC, "raw:secretmanager.secrets.create", "project:{project}", body=on(_fixed("secretmanager.secrets.create", _proj))),
        Route("GET", _SEC + "/{secret}", "raw:secretmanager.secrets.get", res, body=on(_secret_get)),
        Route("PATCH", _SEC + "/{secret}", "raw:secretmanager.secrets.update", res, body=on(_plain_secret("secretmanager.secrets.update"))),
        Route("DELETE", _SEC + "/{secret}", "raw:secretmanager.secrets.delete", res, body=on(_plain_secret("secretmanager.secrets.delete"))),
        Route(
            "POST",
            _SEC + "/{secret}",
            "raw:secretmanager.versions.add",
            res,
            sends_out=True,
            body=on(_iam_verbs("secretmanager.secrets", _secret, "secret", {"addVersion": ("secretmanager.versions.add", None)})),
            description=":addVersion, :getIamPolicy, :setIamPolicy, :testIamPermissions",
        ),
        Route("GET", _SEC + "/{secret}/versions", "raw:secretmanager.versions.list", res, body=on(_plain_secret("secretmanager.versions.list"))),
        Route("GET", _SEC + "/{secret}/versions/{version}", "secret.read", res, body=on(_version("GET")), description="<version> or <version>:access"),
        Route("POST", _SEC + "/{secret}/versions/{version}", "raw:secretmanager.versions.enable", res, body=on(_version("POST"))),
    ]


# -- IAM and IAM Credentials ------------------------------------------------------------------

_SAS = "/v1/projects/{project}/serviceAccounts"


def _account(v: Mapping[str, str]) -> str:
    """serviceaccount:<email>, when the path's project is - or the account's own."""
    email, _ = _verb(v["account"])
    if "@" not in email:
        raise Unreadable("name the service account by its email address, not its unique id")
    resource = f"serviceaccount:{email}"
    try:
        own = full_resource_name(parse_resource(resource)).split("/")[4]
    except (ToolpassError, ResourceError) as e:
        raise Unreadable(f"{email!r} is not a service account toolpass can check ({e})") from None
    if v["project"] not in ("-", own):
        raise Unreadable(f"the path names project {v['project']!r} but the service account belongs to {own!r}; use - for the project")
    return resource


_SA_VERBS = {
    _IAM: {
        "disable": ("iam.serviceAccounts.disable", None),
        "enable": ("iam.serviceAccounts.enable", None),
        "undelete": ("iam.serviceAccounts.undelete", None),
        "signBlob": ("iam.serviceAccounts.signBlob", None),
        "signJwt": ("iam.serviceAccounts.signJwt", None),
    },
    _IAMC: {
        "generateAccessToken": ("iam.serviceAccounts.getAccessToken", None),
        "generateIdToken": ("iam.serviceAccounts.getOpenIdToken", None),
        "signBlob": ("iam.serviceAccounts.signBlob", None),
        "signJwt": ("iam.serviceAccounts.signJwt", None),
    },
}


def _account_post(r: Request, v: Mapping[str, str]) -> Mapping[str, str]:
    """POST <account>:<verb> on iam.googleapis.com (lifecycle, IAM policy,
    signing) or iamcredentials.googleapis.com (tokens, signing). A chain of
    delegates acts through other service accounts too."""
    host = r.host.lower().rstrip(".")
    if host not in _SA_VERBS:
        raise Unreadable(f"{r.host} does not serve this path")
    _, verb = _verb(v["account"])
    if host == _IAM and verb in ("getIamPolicy", "setIamPolicy", "testIamPermissions"):
        return _iam_verbs("iam.serviceAccounts", _account, "account")(r, v)
    if verb not in _SA_VERBS[host]:
        raise Unreadable(f"the method :{verb or '(none)'} is not routed")
    if body_object(r).get("delegates"):
        raise Unreadable("delegates act through other service accounts, each a resource of its own; ask for the token directly")
    permission, effect = _SA_VERBS[host][verb]
    return _check(permission, _account(v), effect)


def _plain_account(permission: str) -> Build:
    def build(r: Request, v: Mapping[str, str]) -> Mapping[str, str]:
        if ":" in v["account"]:
            raise Unreadable(f"{v['account']!r} is not a service account")
        return _check(permission, _account(v))

    return build


def _roles(base: str, var: str, kind: str) -> list[Route]:
    """Custom roles of a project or an organization."""

    def holder(v: Mapping[str, str]) -> str:
        return _project(v[var]) if kind == "project" else f"organization:{v[var]}"

    def one(permission: str) -> Build:
        def build(r: Request, v: Mapping[str, str]) -> Mapping[str, str]:
            if ":" in v["role"]:
                raise Unreadable(f"{v['role']!r} is not a role id")
            return _check(permission, holder(v))

        return build

    def undelete(r: Request, v: Mapping[str, str]) -> Mapping[str, str]:
        _, verb = _verb(v["role"])
        if verb != "undelete":
            raise Unreadable(f"the method :{verb or '(none)'} is not routed on a role")
        return _check("iam.roles.undelete", holder(v))

    res = f"{kind}:{{{var}}}"
    return [
        Route("GET", base + "/roles", "raw:iam.roles.list", res, body=_on(_IAM, _fixed("iam.roles.list", holder))),
        Route("POST", base + "/roles", "raw:iam.roles.create", res, body=_on(_IAM, _fixed("iam.roles.create", holder))),
        Route("GET", base + "/roles/{role}", "raw:iam.roles.get", res, body=_on(_IAM, one("iam.roles.get"))),
        Route("PATCH", base + "/roles/{role}", "raw:iam.roles.update", res, body=_on(_IAM, one("iam.roles.update"))),
        Route("DELETE", base + "/roles/{role}", "raw:iam.roles.delete", res, body=_on(_IAM, one("iam.roles.delete"))),
        Route("POST", base + "/roles/{role}", "raw:iam.roles.undelete", res, body=_on(_IAM, undelete)),
    ]


def _iam() -> list[Route]:
    def on(build: Build) -> Extractor:
        return _on(_IAM, build)

    sa, res = _SAS + "/{account}", "serviceaccount:{account}"
    return [
        Route("GET", _SAS, "raw:iam.serviceAccounts.list", "project:{project}", body=on(_fixed("iam.serviceAccounts.list", _proj))),
        Route("POST", _SAS, "raw:iam.serviceAccounts.create", "project:{project}", body=on(_fixed("iam.serviceAccounts.create", _proj))),
        Route("GET", sa, "raw:iam.serviceAccounts.get", res, body=on(_plain_account("iam.serviceAccounts.get"))),
        Route("PATCH", sa, "raw:iam.serviceAccounts.update", res, body=on(_plain_account("iam.serviceAccounts.update"))),
        Route("PUT", sa, "raw:iam.serviceAccounts.update", res, body=on(_plain_account("iam.serviceAccounts.update"))),
        Route("DELETE", sa, "raw:iam.serviceAccounts.delete", res, body=on(_plain_account("iam.serviceAccounts.delete"))),
        Route(
            "POST",
            sa,
            "raw:iam.serviceAccounts.getAccessToken",
            res,
            sends_out=True,
            body=guarded(_account_post),
            description="iam: lifecycle, IAM policy and signing; iamcredentials: tokens and signing",
        ),
        Route("GET", sa + "/keys", "raw:iam.serviceAccountKeys.list", res, body=on(_plain_account("iam.serviceAccountKeys.list"))),
        Route("POST", sa + "/keys", "raw:iam.serviceAccountKeys.create", res, body=on(_plain_account("iam.serviceAccountKeys.create"))),
        Route("GET", sa + "/keys/{key}", "raw:iam.serviceAccountKeys.get", res, body=on(_plain_account("iam.serviceAccountKeys.get"))),
        Route("DELETE", sa + "/keys/{key}", "raw:iam.serviceAccountKeys.delete", res, body=on(_plain_account("iam.serviceAccountKeys.delete"))),
        Route("GET", "/v1/roles", None, "", untrusted_output=False, body=on(lambda r, v: _unchecked()), description="Google's predefined roles"),
        Route("GET", "/v1/roles/{role}", None, "", untrusted_output=False, body=on(lambda r, v: _unchecked()), description="Google's predefined roles"),
        *_roles("/v1/projects/{project}", "project", "project"),
        *_roles("/v1/organizations/{organization}", "organization", "organization"),
    ]


# -- Compute Engine ---------------------------------------------------------------------------

_GCEP = "/compute/v1/projects/{project}"
_INST = _GCEP + "/zones/{zone}/instances"
_INST_POST = {
    "start": ("compute.instances.start", None),
    "stop": ("compute.instances.stop", None),
    "reset": ("compute.instances.reset", None),
    "suspend": ("compute.instances.suspend", None),
    "resume": ("compute.instances.resume", None),
    "setLabels": ("compute.instances.setLabels", None),
    "setDeletionProtection": ("compute.instances.setDeletionProtection", None),
}
_INST_GET = {
    "getIamPolicy": "compute.instances.getIamPolicy",
    "getSerialPortOutput": "compute.instances.getSerialPortOutput",
    "getGuestAttributes": "compute.instances.getGuestAttributes",
}


def _instance(v: Mapping[str, str]) -> str:
    return f"instance:{v['project']}/{v['zone']}/{v['instance']}"


def _instance_post(r: Request, v: Mapping[str, str]) -> Mapping[str, str]:
    verb = v["verb"]
    if verb == "testIamPermissions":
        return _unchecked()
    if verb not in _INST_POST:
        raise Unreadable(
            f"{verb} is not routed: setMetadata, setServiceAccount, attachDisk and the like run code as the instance's service account or use a second resource"
        )
    permission, effect = _INST_POST[verb]
    return _check(permission, _instance(v), effect)


def _instance_get(r: Request, v: Mapping[str, str]) -> Mapping[str, str]:
    if v["verb"] not in _INST_GET:
        raise Unreadable(f"{v['verb']} is not routed")
    return _check(_INST_GET[v["verb"]], _instance(v))


def _compute() -> list[Route]:
    def on(build: Build) -> Extractor:
        return _on(_GCE, build)

    res = "instance:{project}/{zone}/{instance}"
    out = [
        Route("GET", _GCEP, "raw:compute.projects.get", "project:{project}", body=on(_fixed("compute.projects.get", _proj))),
        Route("GET", _INST, "raw:compute.instances.list", "project:{project}", body=on(_fixed("compute.instances.list", _proj))),
        Route("GET", _GCEP + "/aggregated/instances", "raw:compute.instances.list", "project:{project}", body=on(_fixed("compute.instances.list", _proj))),
        Route(
            "POST",
            _INST,
            None,
            "",
            body=refusing("creating an instance uses a service account, disks, an image and a network, each a resource of its own; not routed"),
        ),
        Route("GET", _INST + "/{instance}", "raw:compute.instances.get", res, body=on(_fixed("compute.instances.get", _instance))),
        Route("DELETE", _INST + "/{instance}", "compute.delete", res, body=on(_fixed("compute.instances.delete", _instance))),
        Route("POST", _INST + "/{instance}/{verb}", "compute.start", res, body=on(_instance_post), description="start, stop, reset, suspend, resume, labels"),
        Route(
            "POST",
            _INST + "/{instance}/setIamPolicy",
            "raw:compute.instances.setIamPolicy",
            res,
            sends_out=True,
            body=on(_fixed("compute.instances.setIamPolicy", _instance)),
        ),
        Route("GET", _INST + "/{instance}/{verb}", "raw:compute.instances.getIamPolicy", res, body=on(_instance_get)),
    ]
    # Operations: the progress of a change, polled or waited on.
    for scope, perm in (
        ("/zones/{zone}", "compute.zoneOperations.get"),
        ("/regions/{region}", "compute.regionOperations.get"),
        ("/global", "compute.globalOperations.get"),
    ):
        op = _GCEP + scope + "/operations/{operation}"
        out += [
            Route("GET", op, "raw:" + perm, "project:{project}", body=on(_fixed(perm, _proj))),
            Route("POST", op + "/wait", "raw:" + perm, "project:{project}", effect="read", body=on(_fixed(perm, _proj))),
        ]
    return out


# -- Cloud Run, GKE -----------------------------------------------------------------------------

_RUNS = "/v2/projects/{project}/locations/{location}/services"


def _service(v: Mapping[str, str]) -> str:
    name, _ = _verb(v["service"])
    return f"service:{v['project']}/{v['location']}/{name}"


def _service_get(r: Request, v: Mapping[str, str]) -> Mapping[str, str]:
    _, verb = _verb(v["service"])
    if verb == "getIamPolicy":
        return _check("run.services.getIamPolicy", _service(v))
    if verb:
        raise Unreadable(f"the method :{verb} is not routed")
    return _check("run.services.get", _service(v))


def _cloud_run() -> list[Route]:
    def on(build: Build) -> Extractor:
        return _on(_RUN, build)

    deploy = refusing("a deploy runs code as the service's service account, which also needs iam.serviceAccounts.actAs on it; one check cannot cover both")

    def delete(r: Request, v: Mapping[str, str]) -> Mapping[str, str]:
        if ":" in v["service"]:
            raise Unreadable(f"{v['service']!r} is not a service name")
        return _check("run.services.delete", _service(v))

    def post(r: Request, v: Mapping[str, str]) -> Mapping[str, str]:
        _, verb = _verb(v["service"])
        if verb == "getIamPolicy":
            raise Unreadable("getIamPolicy is a GET")
        return _iam_verbs("run.services", _service, "service")(r, v)

    res = "service:{project}/{location}/{service}"
    return [
        Route("GET", _RUNS, "raw:run.services.list", "project:{project}", body=on(_fixed("run.services.list", _proj))),
        Route("POST", _RUNS, None, "", body=deploy),
        Route("GET", _RUNS + "/{service}", "raw:run.services.get", res, body=on(_service_get)),
        Route("PATCH", _RUNS + "/{service}", "run.deploy", res, body=deploy),
        Route("DELETE", _RUNS + "/{service}", "raw:run.services.delete", res, body=on(delete)),
        Route(
            "POST", _RUNS + "/{service}", "raw:run.services.setIamPolicy", res, sends_out=True, body=on(post), description=":setIamPolicy, :testIamPermissions"
        ),
        Route(
            "GET",
            "/v2/projects/{project}/locations/{location}/operations/{operation}",
            "raw:run.operations.get",
            "project:{project}",
            body=on(_fixed("run.operations.get", _proj)),
        ),
    ]


def _cluster(loc: str) -> Callable[[Mapping[str, str]], str]:
    def make(v: Mapping[str, str]) -> str:
        if ":" in v["cluster"]:
            raise Unreadable(f"{v['cluster']!r} is not a cluster name")
        return f"cluster:{v['project']}/{v[loc]}/{v['cluster']}"

    return make


def _gke() -> list[Route]:
    def on(build: Build) -> Extractor:
        return _on(_GKE, build)

    out: list[Route] = []
    for scope, loc in (("/locations/{location}", "location"), ("/zones/{zone}", "zone")):
        base = "/v1/projects/{project}" + scope
        res = f"cluster:{{project}}/{{{loc}}}/{{cluster}}"
        out += [
            Route("GET", base + "/clusters", "raw:container.clusters.list", "project:{project}", body=on(_fixed("container.clusters.list", _proj))),
            Route("GET", base + "/clusters/{cluster}", "gke.access", res, body=on(_fixed("container.clusters.get", _cluster(loc)))),
            Route("DELETE", base + "/clusters/{cluster}", "raw:container.clusters.delete", res, body=on(_fixed("container.clusters.delete", _cluster(loc)))),
            Route(
                "GET", base + "/operations/{operation}", "raw:container.operations.get", "project:{project}", body=on(_fixed("container.operations.get", _proj))
            ),
        ]
    return out


# -- Resource Manager -----------------------------------------------------------------------------


def _holder(kind: str, var: str) -> Callable[[Mapping[str, str]], str]:
    def make(v: Mapping[str, str]) -> str:
        name, _ = _verb(v[var])
        return _project(name) if kind == "project" else f"{kind}:{name}"

    return make


def _new_project(r: Request, v: Mapping[str, str]) -> Mapping[str, str]:
    """v3 projects.create: resourcemanager.projects.create on the parent folder or organization."""
    parent = body_object(r).get("parent")
    kind, _, number = parent.partition("/") if isinstance(parent, str) else ("", "", "")
    if kind not in ("folders", "organizations") or not number.isdigit() or not number.isascii():
        raise Unreadable("name the new project's parent (folders/<number> or organizations/<number>)")
    return _check("resourcemanager.projects.create", f"{kind[:-1]}:{number}")


def _resource_manager() -> list[Route]:
    def on(build: Build) -> Extractor:
        return _on(_CRM, build)

    out: list[Route] = []
    for kind, coll, versions in (("project", "projects", ("v1", "v3")), ("folder", "folders", ("v2", "v3")), ("organization", "organizations", ("v1", "v3"))):
        perms = "resourcemanager." + coll
        holder = _holder(kind, "name")
        extra: dict[str, tuple[str, str | None]] = {}
        if kind == "project":
            extra = {"getAncestry": (perms + ".get", "read"), "undelete": (perms + ".undelete", None)}

        def get(r: Request, v: Mapping[str, str], perms: str = perms, holder: Callable[[Mapping[str, str]], str] = holder) -> Mapping[str, str]:
            if ":" in v["name"]:
                raise Unreadable(f"{v['name']!r} is not a resource id")
            return _check(perms + ".get", holder(v))

        for ver in versions:
            path = f"/{ver}/{coll}/{{name}}"
            res = f"{kind}:{{name}}"
            out += [
                Route("GET", path, "project.view" if kind == "project" else f"raw:{perms}.get", res, body=on(get)),
                Route(
                    "POST",
                    path,
                    "iam.set",
                    res,
                    sends_out=True,
                    body=on(_iam_verbs(perms, holder, "name", extra)),
                    description=":getIamPolicy, :setIamPolicy, :testIamPermissions",
                ),
            ]
    for ver in ("v1", "v3"):

        def remove(r: Request, v: Mapping[str, str]) -> Mapping[str, str]:
            if ":" in v["name"]:
                raise Unreadable(f"{v['name']!r} is not a project id")
            return _check("resourcemanager.projects.delete", _project(v["name"]))

        def update(r: Request, v: Mapping[str, str]) -> Mapping[str, str]:
            if ":" in v["name"]:
                raise Unreadable(f"{v['name']!r} is not a project id")
            if "parent" in _canon_keys(body_object(r)):
                raise Unreadable("moving a project changes a second resource (its new parent); not routed")
            return _check("resourcemanager.projects.update", _project(v["name"]))

        out += [
            Route("DELETE", f"/{ver}/projects/{{name}}", "raw:resourcemanager.projects.delete", "project:{name}", body=on(remove)),
            Route("PATCH" if ver == "v3" else "PUT", f"/{ver}/projects/{{name}}", "raw:resourcemanager.projects.update", "project:{name}", body=on(update)),
        ]
    out.append(Route("POST", "/v3/projects", "raw:resourcemanager.projects.create", "folder:{parent}", body=on(_new_project)))
    for path in ("/v1/projects", "/v3/projects", "/v3/projects:search", "/v3/folders", "/v3/folders:search"):
        out.append(Route("GET", path, None, "", body=on(lambda r, v: _unchecked()), description="what the agent's own credential sees; results are untrusted"))
    return out


# -- the table --------------------------------------------------------------------------------------


def _not_routed(r: Request, v: Mapping[str, str]) -> Mapping[str, str]:
    if r.host.lower().rstrip(".") in _SHARED:
        raise Unreadable(
            f"Google Cloud services under {r.host} are not routed; address the service's own host (storage.googleapis.com, bigquery.googleapis.com)"
        )
    raise Unreadable("this Google Cloud request is not routed")


def _routes() -> list[Route]:
    return [
        *_storage(),
        *_bigquery(),
        *_secret_manager(),
        *_iam(),
        *_compute(),
        *_cloud_run(),
        *_gke(),
        *_resource_manager(),
        # Everything else read on these hosts is refused, not passed unseen.
        *(Route(m, "/{rest+}", None, "", body=guarded(_not_routed), spec=False) for m in ("GET", "HEAD")),
    ]


def _locate(settings: Any) -> list[tuple[str, str]]:
    """Google Cloud's service hosts, and the same services' paths under the
    shared www and content hosts. The connection's own settings name only
    the Policy Troubleshooter, which toolpass calls itself."""
    out = [(h, "") for h in HOSTS]
    out += [(shared, alias) for shared in _SHARED for alias in _ALIASES]
    return out


ROUTES = RouteTable(
    "googlecloud",
    # No route is in a vendor description toolpass holds (only the troubleshooter's is).
    [dataclasses.replace(r, spec=False, body=r.body or guarded()) for r in _routes()],
    claims=("/v3/iam:troubleshoot",),
    unrouted={
        ("POST", "/v3/iam:troubleshoot"): "tells which permissions any principal holds on any resource; toolpass asks it itself, a tool does not",
    },
    locate=_locate,
)

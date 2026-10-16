"""Salesforce REST requests, mapped to the salesforce integration's actions.

A record by Id (``/services/data/vNN.N/sobjects/{Type}/{Id}``) is
``record.read``, ``record.edit`` or ``record.delete`` on ``record:<Id>``;
a change of ``OwnerId`` is ``record.transfer``. Creating records is
``object.create`` on ``object:<Type>``, one record or many of one type
(``composite/tree``, ``composite/sobjects``). A SOQL query (``/query``,
``/queryAll``) is read with a small parser: ``WHERE Id = '<Id>'`` alone is
``record.read`` on that record; any other query may return any record of
its object, so it is ``object.view_all`` on ``object:<Type>``. Field-level
security is not checked separately.

Refused: a query the parser cannot read exactly (a subquery or semi-join, a
relationship field such as ``Owner.Email``, TYPEOF, FOR, WITH, USING), a body
that names another record (a lookup or a nested record, which would need a
second check: an Id in a field whose name ends in Id, RecordTypeId included,
or an Id-like value in a custom field, which may be a lookup), a record by
external id, related lists, composite and batch
requests that bundle several, method overrides, and everything else the
instance serves (Apex REST, UI API, Chatter, Tooling, Bulk, file downloads).
"""

from __future__ import annotations

import json
import re
import urllib.parse
from collections.abc import Callable, Mapping
from typing import Any

from toolpass.core.routes import Extractor, Request, Route, RouteTable
from toolpass.integrations.salesforce.soql import API_NAME_RE, ID_RE

__all__ = ["ROUTES", "read_soql"]

_RECORD = "record:{record}"
_OBJECT = "object:{type}"
_ANY = "{resource}"
_D = "/services/data/{version}"

_VERSION_RE = re.compile(r"v[0-9]{2,3}\.[0-9]")
# Binary fields read from a record's own path (an Attachment's Body, a
# ContentVersion's VersionData); any other name there is a related list or
# an external id field.
_BLOB_FIELDS = frozenset({"Body", "VersionData"})
_Ex = Callable[[Request, Mapping[str, str]], dict[str, str]]


class _Refused(Exception):
    """Raised by a helper to refuse the request with this reason."""


def _guard(r: Request) -> str | None:
    """What every route refuses: method overrides (Salesforce honours
    _HttpMethod on POST), and paths the server may read differently (dot
    segments, encoded slashes, ;parameters)."""
    if "x-http-method-override" in r.headers or any(k.strip().lower() == "_httpmethod" for k, _ in urllib.parse.parse_qsl(r.query, keep_blank_values=True)):
        return "a method override is not routed; send the request with its own method"
    low = r.raw_path.lower()
    if "%2f" in low or "%5c" in low or "\\" in r.raw_path or ";" in r.raw_path or "%3b" in low:
        return "an encoded slash or a ; in the path is not routed"
    if any(s in (".", "..") for s in r.path.split("/")):
        return "a dot segment in the path is not routed"
    return None


def _route(method: str, path: str, action: str | None, resource: str, body: Extractor | None = None, **kw: Any) -> Route:
    """A route whose extractor runs the shared guard and checks the API
    version first."""

    def extract(r: Request, vars_: Mapping[str, str]) -> Mapping[str, str] | None:
        why = _guard(r)
        if why is not None:
            return {"_unresolved": why}
        if "version" in vars_ and not _VERSION_RE.fullmatch(vars_["version"]):
            return {"_unresolved": f"{vars_['version']!r} is not an API version such as v66.0"}
        try:
            return body(r, vars_) if body is not None else {}
        except _Refused as e:
            return {"_unresolved": str(e)}

    return Route(method, path, action, resource, body=extract, spec=False, **kw)


def _refuse(reason: str) -> Extractor:
    def extract(r: Request, vars_: Mapping[str, str]) -> Mapping[str, str]:
        raise _Refused(reason)

    return extract


def _looks_like_id(s: str) -> bool:
    """A Salesforce Id: 15 or 18 letters and digits, with digits (every Id's
    pod and reserved characters are; a plain word is not an Id)."""
    return bool(ID_RE.fullmatch(s)) and any(c.isdigit() for c in s)


def _type(name: str) -> str:
    if not API_NAME_RE.fullmatch(name):
        raise _Refused(f"{name!r} is not an object API name")
    return name


def _record_id(s: str) -> str:
    if not _looks_like_id(s):
        raise _Refused(f"{s!r} is not a record Id; address the record by its 15- or 18-character Id")
    return s


# -- bodies -------------------------------------------------------------------


def _strict_json(r: Request) -> Any:
    """The body as JSON, read strictly: a JSON content type, no duplicate
    member names (compared without case, as Salesforce field names are), no
    NaN. Keys come back lowercased."""
    if not r.body:
        raise _Refused("the request body could not be read; send it as JSON bytes")
    ct = r.content_type
    if ct != "application/json" and not ct.endswith("+json"):
        raise _Refused("the body must be JSON (application/json)")

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for k, v in items:
            if k.lower() in out:
                raise _Refused(f"the field {k!r} is given more than once")
            out[k.lower()] = v
        return out

    def no_constant(name: str) -> Any:
        raise _Refused(f"{name} is not JSON")

    try:
        return json.loads(r.body.decode("utf-8"), object_pairs_hook=pairs, parse_constant=no_constant)
    except (ValueError, UnicodeDecodeError):
        raise _Refused("the body is not JSON") from None


def _names_record(k: str, v: str) -> bool:
    """Whether the value of field ``k`` (lowercased) sets a lookup to
    another record. Standard reference fields end in Id (AccountId,
    ParentId, RecordTypeId): any 15- or 18-character Id there is a lookup.
    A custom or namespaced field (Manager__c) may be a lookup too, which
    its name does not show, so an Id-like value there is one. Other
    standard fields are text, whatever their value looks like."""
    v = v.strip()
    if k.endswith("id"):
        return bool(ID_RE.fullmatch(v))
    return "__" in k and _looks_like_id(v)


def _fields(rec: Any, typ: str, *, creating: bool) -> bool:
    """Check one record's fields; True when it changes OwnerId. A lookup
    (see _names_record), a nested object or a list names another record (a
    parent by Id or external id, child records), which one check cannot
    cover. RecordTypeId is such a lookup: whether the session's user may
    use a record type has no check, so it is refused like the others."""
    if not isinstance(rec, dict):
        raise _Refused("a record must be a JSON object")
    owner = False
    for k, v in rec.items():
        if k == "attributes":
            t = v.get("type") if isinstance(v, dict) else None
            if not isinstance(v, dict) or (t is not None and (not isinstance(t, str) or t.lower() != typ.lower())):
                raise _Refused(f"attributes must name the type {typ}")
            continue
        if not API_NAME_RE.fullmatch(k):
            raise _Refused(f"{k!r} is not a field API name")
        if k == "id":
            raise _Refused("leave Id out of the body: the path names the record")
        if k == "ownerid":
            if creating:
                raise _Refused("creating a record owned by someone else is not routed; create it, then change OwnerId")
            owner = True
            continue
        if isinstance(v, (dict, list)):
            raise _Refused(f"{k} holds a nested record or a list; toolpass checks one record per request")
        if isinstance(v, str) and _names_record(k, v):
            raise _Refused(f"{k} names another record ({v}); toolpass checks one record per request")
    return owner


def _create(r: Request, vars_: Mapping[str, str]) -> dict[str, str]:
    typ = _type(vars_["type"])
    _fields(_strict_json(r), typ, creating=True)
    return {}


def _update(r: Request, vars_: Mapping[str, str]) -> dict[str, str]:
    _type(vars_["type"])
    out = {"record": _record_id(vars_["record"])}
    if _fields(_strict_json(r), vars_["type"], creating=False):
        out["_action"] = "record.transfer"
    return out


def _records(doc: Any, allowed: frozenset[str]) -> list[Any]:
    if not isinstance(doc, dict) or set(doc) - allowed:
        raise _Refused(f"the body may hold only {', '.join(sorted(allowed))}")
    recs = doc.get("records")
    if not isinstance(recs, list) or not recs:
        raise _Refused("records must be a non-empty list")
    return recs


def _tree(r: Request, vars_: Mapping[str, str]) -> dict[str, str]:
    """composite/tree/{Type}: records of that type, without child records
    (another object) or references."""
    typ = _type(vars_["type"])
    for rec in _records(_strict_json(r), frozenset({"records"})):
        _fields(rec, typ, creating=True)
    return {}


def _collection(r: Request, vars_: Mapping[str, str]) -> dict[str, str]:
    """POST composite/sobjects: records created, all of one type."""
    recs = _records(_strict_json(r), frozenset({"records", "allornone"}))
    types = set()
    for rec in recs:
        attrs = rec.get("attributes") if isinstance(rec, dict) else None
        t = attrs.get("type") if isinstance(attrs, dict) else None
        if not isinstance(t, str):
            raise _Refused("each record needs attributes.type")
        types.add(t.lower())
    if len(types) != 1:
        raise _Refused("records of several types need one check each; send one type per request")
    typ = _type(recs[0]["attributes"]["type"])
    for rec in recs:
        _fields(rec, typ, creating=True)
    return {"type": typ}


# -- reads --------------------------------------------------------------------


def _record(r: Request, vars_: Mapping[str, str]) -> dict[str, str]:
    _type(vars_["type"])
    return {"record": _record_id(vars_["record"])}


def _blob(r: Request, vars_: Mapping[str, str]) -> dict[str, str]:
    out = _record(r, vars_)
    if vars_["field"] not in _BLOB_FIELDS:
        raise _Refused(f"{vars_['field']!r} below a record is a related list or an external id; query it with its own Id")
    return out


def _object(r: Request, vars_: Mapping[str, str]) -> dict[str, str]:
    return {"type": _type(vars_["type"])}


_DATE_RE = re.compile(r"\d+(\.\d+)?|\d{4}-\d{2}-\d{2}(T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:?\d{2}))?")
# Keywords that make a query do something else than read its one object:
# locking or tracking (FOR UPDATE, UPDATE VIEWSTAT), polymorphic fields,
# scopes and data categories.
_FORBIDDEN = frozenset({"typeof", "for", "with", "using", "update", "tracking", "viewstat"})
_AFTER_FROM = frozenset({"where", "group", "order", "limit", "offset"})


def _tokens(q: str) -> list[tuple[str, str]]:
    """SOQL as (kind, text) tokens: ident, string, number, punct. What the
    tokenizer does not know is refused, so the parser and Salesforce cannot
    disagree about where a string or a name ends."""
    out: list[tuple[str, str]] = []
    i, n = 0, len(q)
    while i < n:
        c = q[i]
        if c in " \t\r\n":
            i += 1
        elif c == "'":
            j = i + 1
            while j < n and q[j] != "'":
                j += 2 if q[j] == "\\" else 1
            if j >= n:
                raise _Refused("the query has an unterminated string")
            out.append(("string", q[i + 1 : j]))
            i = j + 1
            if i < n and q[i] == "'":
                raise _Refused("adjacent strings in the query are not routed")
        elif c.isascii() and (c.isalpha() or c == "_"):
            j = i
            while j < n and q[j].isascii() and (q[j].isalnum() or q[j] == "_"):
                j += 1
            if j < n and q[j] == ".":
                raise _Refused("a relationship field (Parent.Field) reads another object; query that object by Id")
            out.append(("ident", q[i:j]))
            i = j
        elif c.isascii() and c.isdigit():
            j = i
            while j < n and ((q[j].isascii() and q[j].isalnum()) or q[j] in ":.+-"):
                j += 1
            if not _DATE_RE.fullmatch(q[i:j]):
                raise _Refused(f"{q[i:j]!r} in the query is not a number or a date")
            out.append(("number", q[i:j]))
            i = j
        elif c in "(),=<>!:+-":
            out.append(("punct", c))
            i += 1
        else:
            raise _Refused(f"the character {c!r} in the query is not routed")
    return out


def read_soql(q: str) -> tuple[str, str]:
    """What a SOQL query reads: ("record", Id) for ``WHERE Id = '<Id>'``
    alone, else ("object", Type). Raises ValueError for a query it cannot
    read exactly."""
    try:
        return _read_soql(q)
    except _Refused as e:
        raise ValueError(str(e)) from None


def _read_soql(q: str) -> tuple[str, str]:
    toks = _tokens(q)
    words = [t.lower() if k == "ident" else "" for k, t in toks]
    if not toks or words[0] != "select":
        raise _Refused("the query must start with SELECT")
    if words.count("select") != 1:
        raise _Refused("a subquery or semi-join reads another object; query it on its own")
    if words.count("from") != 1:
        raise _Refused("the query must have one FROM")
    bad = _FORBIDDEN.intersection(words)
    if bad:
        raise _Refused(f"{sorted(bad)[0].upper()} is not routed in a query")
    at = words.index("from")
    if at < 2 or at + 1 >= len(toks) or toks[at + 1][0] != "ident":
        raise _Refused("the query must select fields FROM one object")
    typ = _type(toks[at + 1][1])
    rest = toks[at + 2 :]
    if rest and (rest[0][0] != "ident" or rest[0][1].lower() not in _AFTER_FROM):
        raise _Refused("only WHERE, GROUP BY, ORDER BY, LIMIT and OFFSET may follow the object")
    if any(k == "string" for k, _ in toks[:at]):
        raise _Refused("a string in the field list is not routed")
    # WHERE Id = '<Id>' [LIMIT n]: exactly one record.
    shape = [(k, t.lower() if k == "ident" else t) for k, t in rest]
    one = len(shape) in (4, 6) and shape[:3] == [("ident", "where"), ("ident", "id"), ("punct", "=")] and shape[3][0] == "string"
    if one and (len(shape) == 4 or (shape[4] == ("ident", "limit") and shape[5][0] == "number")) and _looks_like_id(shape[3][1]):
        return "record", shape[3][1]
    return "object", typ


def _query(r: Request, vars_: Mapping[str, str]) -> dict[str, str]:
    params = urllib.parse.parse_qsl(r.query, keep_blank_values=True)
    if [k for k, _ in params] != ["q"]:
        raise _Refused("a query request carries one q parameter and nothing else")
    q = params[0][1]
    if any((c < " " and c not in "\t\r\n") or c == "\x7f" for c in q):
        raise _Refused("the query contains a control character")
    kind, value = _read_soql(q)
    if kind == "record":
        return {"resource": f"record:{value}", "_action": "record.read"}
    return {"resource": f"object:{value}"}


# -- the table ----------------------------------------------------------------


def _routes() -> list[Route]:
    other = "this part of the Salesforce API is not routed; use sobjects and query"
    return [
        _route("GET", "/**", None, "", body=_refuse(other), description="the rest of the instance: Apex REST, file downloads, Bulk results"),
        _route("POST", "/services/oauth2/token", None, "", untrusted_output=False, description="the agent's own access token"),
        _route("GET", "/services/oauth2/userinfo", None, "", untrusted_output=False, description="the agent's own identity"),
        _route("GET", "/services/data", None, "", untrusted_output=False, description="the API versions"),
        _route("GET", _D, None, "", untrusted_output=False, description="the API's resources"),
        _route("GET", _D + "/limits", None, "", untrusted_output=False, description="the org's API limits"),
        _route("GET", _D + "/{rest+}", None, "", body=_refuse(other)),
        _route("GET", _D + "/sobjects", None, "", untrusted_output=False, description="the org's schema"),
        _route("GET", _D + "/sobjects/{type}", None, "", body=_refuse("the object's summary lists recent records; use /sobjects/{Type}/describe")),
        _route("GET", _D + "/sobjects/{type}/describe", "object.read", _OBJECT, body=_object, description="the object's schema"),
        _route("GET", _D + "/sobjects/{type}/describe/{rest+}", "object.read", _OBJECT, body=_object, description="the object's layouts"),
        _route("GET", _D + "/sobjects/{type}/updated", "object.view_all", _OBJECT, body=_object, description="the Ids of every changed record"),
        _route("GET", _D + "/sobjects/{type}/deleted", "object.view_all", _OBJECT, body=_object, description="the Ids of every deleted record"),
        _route("GET", _D + "/sobjects/{type}/{record}", "record.read", _RECORD, body=_record),
        _route("GET", _D + "/sobjects/{type}/{record}/{field}", "record.read", _RECORD, body=_blob, description="a binary field of the record"),
        _route("GET", _D + "/query", "object.view_all", _ANY, body=_query, description="SOQL; WHERE Id = '<Id>' alone reads that record"),
        _route("GET", _D + "/queryAll", "object.view_all", _ANY, body=_query, description="SOQL with deleted and archived records"),
        _route("POST", _D + "/sobjects/{type}", "object.create", _OBJECT, body=_create),
        _route("PATCH", _D + "/sobjects/{type}/{record}", "record.edit", _RECORD, body=_update, description="record.transfer when OwnerId changes"),
        _route("DELETE", _D + "/sobjects/{type}/{record}", "record.delete", _RECORD, body=_record),
        _route("POST", _D + "/composite/tree/{type}", "object.create", _OBJECT, body=_tree),
        _route("POST", _D + "/composite/sobjects", "object.create", _OBJECT, body=_collection),
        _route("*", _D + "/composite", None, "", body=_refuse("a composite request bundles several; send each request on its own")),
        _route("*", _D + "/composite/{rest+}", None, "", body=_refuse("a composite or batch request bundles several; send each request on its own")),
    ]


ROUTES = RouteTable(
    "salesforce",
    _routes(),
    claims=("/services/data",),
    unrouted={
        ("PATCH", "/services/data/{version}/sobjects/{type}/{field}/{value}"): "an upsert by external id may create or change a record whose Id is not known",
        ("DELETE", "/services/data/{version}/sobjects/{type}/{field}/{value}"): "a record by external id: address it by Id",
        ("*", "/services/data/{version}/actions/**"): "invocable actions (flows, email, Apex) run as the agent and do what they are built to do",
        ("*", "/services/data/{version}/process/**"): "approvals and process rules have no check",
        ("*", "/services/data/{version}/jobs/**"): "Bulk API jobs span many records",
        ("*", "/services/data/{version}/tooling/**"): "the Tooling API changes metadata, which has no check",
        ("*", "/services/data/{version}/ui-api/**"): "the UI API: use sobjects",
        ("*", "/services/data/{version}/chatter/**"): "Chatter has no check",
        ("*", "/services/data/{version}/connect/**"): "Connect has no check",
    },
)

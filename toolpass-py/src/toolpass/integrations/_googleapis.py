"""Reading Google API requests the way Google's front end reads them, for
the googleworkspace and googlecloud route tables.

Google's APIs share one front end: the same query parameters (``alt``,
``uploadType``), the same ``X-HTTP-Method-Override`` header, the same
upload protocols (a raw body, a ``multipart/related`` body whose first part
is the JSON metadata, or a resumable session opened with JSON metadata and
continued with ``upload_id``). These helpers read a request only where the
reading is unambiguous, and say why otherwise; the tables refuse then.
"""

from __future__ import annotations

import json
import re
import urllib.parse
from collections.abc import Mapping
from typing import Any

from toolpass.core.routes import Extractor, Request

__all__ = [
    "UPLOAD_TYPES",
    "Unreadable",
    "body_object",
    "guarded",
    "multipart_metadata",
    "param",
    "refusing",
    "upload_metadata",
]

UPLOAD_TYPES = ("media", "multipart", "resumable")


class Unreadable(ValueError):
    """The request cannot be read exactly; the message says why."""


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in pairs:
        if k in out:
            raise Unreadable(f"the JSON body gives {k!r} more than once")
        out[k] = v
    return out


def _constant(name: str) -> Any:
    raise Unreadable(f"the JSON body holds {name}, which is not JSON")


def _strict_json(text: bytes) -> dict[str, Any]:
    """A JSON object, with no key given twice anywhere (Python keeps the
    last, other parsers the first) and no NaN or Infinity."""
    try:
        doc = json.loads(text.decode("utf-8"), object_pairs_hook=_no_duplicates, parse_constant=_constant)
    except (UnicodeDecodeError, ValueError) as e:
        if isinstance(e, Unreadable):
            raise
        raise Unreadable("the body is not valid JSON") from None
    if not isinstance(doc, dict):
        raise Unreadable("the JSON body is not an object")
    return doc


def _has_unread_body(r: Request) -> bool:
    """A body the hook did not see (a stream or a file), going by the headers."""
    h = r.headers
    return h.get("content-length", "0").strip() not in ("", "0") or "transfer-encoding" in h


def body_object(r: Request) -> dict[str, Any]:
    """The request's JSON object body, or {} when it has none. Raises
    Unreadable for any other content type, a body the hook could not see,
    or JSON a different parser could read differently."""
    if r.body is None:
        if _has_unread_body(r):
            raise Unreadable("the request body is a stream toolpass cannot read")
        return {}
    if not r.body.strip():
        return {}
    if r.content_type != "application/json":
        raise Unreadable(f"the body must be application/json, not {r.content_type or 'untyped'}")
    return _strict_json(r.body)


def param(r: Request, name: str) -> str | None:
    """A query parameter's value, None when absent; raises Unreadable when
    it is given more than once."""
    values = urllib.parse.parse_qs(r.query, keep_blank_values=True).get(name)
    if not values:
        return None
    if len(values) > 1:
        raise Unreadable(f"the query gives {name} more than once")
    return values[0]


def upload_metadata(r: Request) -> tuple[str, dict[str, Any]]:
    """(uploadType, metadata) of a request to an ``/upload/...`` path.

    ``media`` carries only content (metadata {}); ``multipart`` carries the
    metadata as the first part of a multipart/related body; ``resumable``
    opens a session with a JSON body, or continues one (``upload_id``)
    with content only."""
    kind = param(r, "uploadType")
    if kind not in UPLOAD_TYPES:
        raise Unreadable(f"uploadType must be one of {', '.join(UPLOAD_TYPES)}")
    if kind == "media" or (kind == "resumable" and param(r, "upload_id") is not None):
        return kind, {}
    if kind == "resumable":
        return kind, body_object(r)
    return kind, multipart_metadata(r)


_HEADER_RE = re.compile(rb"([A-Za-z0-9-]+):[ \t]*(.*?)[ \t]*")


def multipart_metadata(r: Request) -> dict[str, Any]:
    """The JSON metadata that opens a multipart/related upload body.

    Read strictly, so no reading of the body other than MIME's can find
    other metadata: the body starts with the delimiter (no preamble), the
    first part's headers are plain ``Name: value`` lines naming
    application/json and no transfer encoding but an identity one, and the
    part ends at the first line that starts with the delimiter, which must
    be followed by a line end or ``--``."""
    if r.content_type != "multipart/related":
        raise Unreadable(f"a multipart upload must be multipart/related, not {r.content_type or 'untyped'}")
    boundary = r.content_type_params.get("boundary", "")
    if not boundary or len(boundary) > 70:
        raise Unreadable("the multipart body has no usable boundary")
    if r.body is None:
        raise Unreadable("the request body is a stream toolpass cannot read")
    delim = b"--" + boundary.encode("latin-1")
    body = r.body
    if not body.startswith(delim):
        raise Unreadable("the multipart body must start with its boundary")
    rest = body[len(delim) :]
    eol = b"\r\n" if rest.startswith(b"\r\n") else b"\n" if rest.startswith(b"\n") else b""
    if not eol:
        raise Unreadable("the multipart boundary line is not followed by a line end")
    rest = rest[len(eol) :]
    if rest.startswith(eol):
        raise Unreadable("the first multipart part has no headers; it must be the application/json metadata")
    head, sep, rest = rest.partition(eol + eol)
    if not sep:
        raise Unreadable("the first multipart part has no header end")
    seen: dict[str, str] = {}
    for line in head.split(eol) if head else []:
        m = _HEADER_RE.fullmatch(line)
        if m is None or b"\r" in line:
            raise Unreadable("a multipart part header is not a plain Name: value line")
        name = m.group(1).decode("ascii").lower()
        if name in seen:
            raise Unreadable(f"the metadata part gives {name} more than once")
        seen[name] = m.group(2).decode("latin-1")
    ctype, _, params = seen.get("content-type", "").partition(";")
    if ctype.strip().lower() != "application/json" or params.strip().lower() not in ("", "charset=utf-8", 'charset="utf-8"'):
        raise Unreadable("the first multipart part must be the application/json metadata")
    if seen.get("content-transfer-encoding", "binary").strip().lower() not in ("7bit", "8bit", "binary"):
        raise Unreadable("the metadata part must not be transfer-encoded")
    end = rest.find(b"\n" + delim)
    if end < 0:
        raise Unreadable("the metadata part has no closing boundary")
    after = rest[end + 1 + len(delim) :]
    if not (after.startswith((b"\r\n", b"\n", b"--"))):
        raise Unreadable("the boundary after the metadata part is followed by other text")
    text = rest[:end]
    if text.endswith(b"\r"):
        text = text[:-1]
    if not text.strip():
        return {}
    return _strict_json(text)


def guarded(extract: Extractor | None = None) -> Extractor:
    """An extractor that first refuses what every Google route must refuse:
    a path or query that is not UTF-8 once decoded (the hook keeps such a
    segment encoded, Google decodes it: two names for one request),
    ``X-HTTP-Method-Override`` (Google acts on the overriding method, the
    route matched the sent one) and ``useDomainAdminAccess`` or
    ``useAdminAccess`` (the request then acts with the agent's
    administrator rights, which the user's check does not cover). An
    Unreadable raised inside becomes the refusal's reason."""

    def run(r: Request, vars_: Mapping[str, str]) -> Mapping[str, str] | None:
        try:
            if not _utf8(r.raw_path) or not _utf8(r.query):
                return {"_unresolved": "the path or query is not valid UTF-8 once decoded"}
            if "x-http-method-override" in r.headers:
                return {"_unresolved": "X-HTTP-Method-Override changes the method Google acts on; send the method itself"}
            for admin in ("useDomainAdminAccess", "useAdminAccess"):
                if (param(r, admin) or "false").lower() != "false":
                    return {"_unresolved": f"{admin} acts with the agent's administrator rights; toolpass checks the user's own access"}
            return extract(r, vars_) if extract is not None else {}
        except Unreadable as e:
            return {"_unresolved": str(e)}

    return run


def _utf8(raw: str) -> bool:
    try:
        urllib.parse.unquote_to_bytes(raw).decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def refusing(reason: str) -> Extractor:
    """An extractor that always refuses, with the reason."""

    def run(r: Request, vars_: Mapping[str, str]) -> Mapping[str, str]:
        return {"_unresolved": reason}

    return run

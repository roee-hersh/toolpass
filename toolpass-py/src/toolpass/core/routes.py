"""Routes: what an HTTP request to an integration's API means in toolpass's
vocabulary.

A route maps a method and a path template of a vendor API to the action and
resource the permission engine answers for, and classifies the request as a
read, a write or a destructive action:

    Route("POST", "/repos/{owner}/{repo}/pulls", "pr.create", "repo:{owner}/{repo}")

The secure-tools toolkit matches every request a tool's body makes against
the tables the integrations declare, so the actions a tool performs are
observed, not declared. The same matcher serves the tests' API-description
checks (tests/harness/spec.py).

Path templates are made of ``/``-separated segments. ``{name}`` matches one
non-empty segment and captures it. ``{name+}`` matches one or more segments
to the end of the path. ``**`` as the last segment matches anything from
there on, captured nowhere. When several templates match, the one with the
most literal segments wins (``/repos/{o}/{r}/pulls`` over
``/repos/{o}/{r}/{rest+}``); ties are broken by the template text, so the
choice is stable.
"""

from __future__ import annotations

import json
import re
import urllib.parse
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "EFFECTS",
    "Extractor",
    "Locator",
    "Observed",
    "PathTemplate",
    "Request",
    "Route",
    "RouteTable",
    "best_templates",
    "effect_of_method",
    "endpoint_of_url",
    "fill",
    "host_key",
    "matching_templates",
    "rank",
    "stronger",
    "template_fields",
]

EFFECTS: tuple[str, ...] = ("read", "write", "destructive")
_RANK = {e: i for i, e in enumerate(EFFECTS)}
_FIELD_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


def rank(effect: str | None) -> int:
    """read < write < destructive; an unknown or missing effect ranks lowest."""
    return _RANK.get(effect or "", 0)


def stronger(a: str | None, b: str | None) -> str | None:
    """The stronger of two effects (destructive > write > read); None when both are None."""
    if a is None:
        return b
    if b is None:
        return a
    return a if _RANK.get(a, 0) >= _RANK.get(b, 0) else b


def effect_of_method(method: str) -> str:
    """The default classification of an HTTP method."""
    m = method.upper()
    if m in ("GET", "HEAD", "OPTIONS"):
        return "read"
    if m == "DELETE":
        return "destructive"
    return "write"


def template_fields(template: str) -> tuple[str, ...]:
    """The ``{name}`` fields of a template, in order, without duplicates."""
    out: list[str] = []
    for name in _FIELD_RE.findall(template):
        if name not in out:
            out.append(name)
    return tuple(out)


def fill(template: str, values: Mapping[str, str]) -> str | None:
    """``template`` with every ``{name}`` replaced from ``values``; None when a
    field is missing. An empty value fills as empty (an optional suffix)."""

    def one(m: re.Match[str]) -> str:
        v = values.get(m.group(1))
        if v is None:
            raise KeyError(m.group(1))
        return v

    try:
        return _FIELD_RE.sub(one, template)
    except KeyError:
        return None


@dataclass(frozen=True)
class Request:
    """One outbound HTTP request as the hook sees it, before it is sent.

    ``path`` is percent-decoded per segment; ``raw_path`` is as sent.
    ``headers`` keys are lowercased. ``body`` is the request body when it
    was bytes or text, else None (a stream or file is not read)."""

    scheme: str
    host: str
    port: int | None
    method: str
    raw_path: str
    query: str = ""
    headers: Mapping[str, str] = field(default_factory=dict)
    body: bytes | None = None

    @property
    def path(self) -> str:
        return "/".join(_decode_segment(s) for s in self.raw_path.split("/"))

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "").split(";", 1)[0].strip().lower()

    def json(self) -> Any:
        """The body as JSON, or None when it is not JSON."""
        if not self.body:
            return None
        try:
            return json.loads(self.body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None

    def form(self) -> dict[str, str]:
        """The body as a URL-encoded or multipart form (first value per key,
        file parts left out), or {}."""
        if not self.body or "json" in self.content_type:
            return {}
        if "multipart" in self.content_type:
            return _multipart_fields(self.body, self.content_type_params.get("boundary", ""))
        try:
            text = self.body.decode("utf-8")
        except UnicodeDecodeError:
            return {}
        if "\n" in text and "=" not in text:
            return {}
        return {k: v[0] for k, v in urllib.parse.parse_qs(text, keep_blank_values=True).items()}

    @property
    def content_type_params(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for part in self.headers.get("content-type", "").split(";")[1:]:
            k, _, v = part.strip().partition("=")
            out[k.lower()] = v.strip('"')
        return out

    def params(self) -> dict[str, str]:
        """Query parameters (first value per key)."""
        return {k: v[0] for k, v in urllib.parse.parse_qs(self.query, keep_blank_values=True).items()}

    def ambiguous(self) -> set[str]:
        """The names given more than once, or both in the query and in the
        body with different values: a route must not pick one, because the
        server may pick the other."""
        out: set[str] = set()
        query = urllib.parse.parse_qs(self.query, keep_blank_values=True)
        out.update(k for k, v in query.items() if len(v) > 1)
        form: dict[str, list[str]] = {}
        if self.body and "json" not in self.content_type and "multipart" not in self.content_type:
            try:
                form = urllib.parse.parse_qs(self.body.decode("utf-8"), keep_blank_values=True)
            except UnicodeDecodeError:
                form = {}
            out.update(k for k, v in form.items() if len(v) > 1)
        elif self.body and "multipart" in self.content_type:
            out.update(_multipart_duplicates(self.body, self.content_type_params.get("boundary", "")))
        body_values = {k: v[0] for k, v in form.items()}
        doc = self.json()
        if isinstance(doc, dict):
            body_values.update({str(k): str(v) for k, v in doc.items() if isinstance(v, (str, int, float))})
        for k, v in query.items():
            if k in body_values and body_values[k] != v[0]:
                out.add(k)
        return out

    def fields(self) -> dict[str, str]:
        """Every named value the request carries: query, then form, then the
        top-level string, number and boolean members of a JSON object body.
        Body values win over query values."""
        out = self.params()
        out.update(self.form())
        doc = self.json()
        if isinstance(doc, dict):
            for k, v in doc.items():
                if isinstance(v, (str, int, float)) and not isinstance(v, bool):
                    out[str(k)] = str(v)
                elif isinstance(v, bool):
                    out[str(k)] = "true" if v else "false"
        return out

    def text(self) -> list[str]:
        """The request's text, for the untrusted-input check: the path, the
        query and the body when it is text."""
        out = [self.path]
        if self.query:
            out.append(urllib.parse.unquote_plus(self.query))
        if self.body:
            try:
                out.append(self.body.decode("utf-8"))
            except UnicodeDecodeError:
                pass
        return out

    def describe(self) -> str:
        port = "" if self.port in (None, 80, 443) else f":{self.port}"
        return f"{self.method} {self.scheme}://{self.host}{port}{self.raw_path}"


def _decode_segment(s: str) -> str:
    try:
        return urllib.parse.unquote(s, errors="strict")
    except UnicodeDecodeError:
        return s


def _multipart_duplicates(body: bytes, boundary: str) -> set[str]:
    """The non-file field names that appear more than once in a multipart body."""
    if not boundary:
        return set()
    seen: set[str] = set()
    out: set[str] = set()
    for part in body.split(b"--" + boundary.encode()):
        head, sep, _ = part.partition(b"\r\n\r\n")
        if not sep or b"filename=" in head:
            continue
        m = re.search(rb'name="([^"]+)"', head)
        if m is None:
            continue
        name = m.group(1).decode("utf-8", "replace")
        if name in seen:
            out.add(name)
        seen.add(name)
    return out


def _multipart_fields(body: bytes, boundary: str) -> dict[str, str]:
    """The simple (non-file) fields of a multipart body."""
    if not boundary:
        return {}
    out: dict[str, str] = {}
    for part in body.split(b"--" + boundary.encode()):
        head, sep, data = part.partition(b"\r\n\r\n")
        if not sep:
            continue
        m = re.search(rb'name="([^"]+)"', head)
        if m is None or b"filename=" in head:
            continue
        try:
            out[m.group(1).decode()] = data.rstrip(b"\r\n").decode("utf-8")
        except UnicodeDecodeError:
            continue
    return out


class PathTemplate:
    """One path template, matched segment by segment."""

    __slots__ = ("literals", "names", "raw", "segments", "tail")

    def __init__(self, template: str) -> None:
        self.raw = template
        self.segments: list[str] = []  # a literal, "{name}" or "{name+}"
        self.names: list[str] = []
        self.literals = 0
        self.tail: str | None = None  # the "{name+}" or "**" at the end, if any
        parts = template.strip("/").split("/") if template.strip("/") else []
        for i, seg in enumerate(parts):
            last = i == len(parts) - 1
            if seg == "**":
                if not last:
                    raise ValueError(f"route template {template!r}: ** may only end the template")
                self.tail = seg
            elif seg.startswith("{") and seg.endswith("+}"):
                if not last:
                    raise ValueError(f"route template {template!r}: {seg} may only end the template")
                self.tail = seg
                self.names.append(seg[1:-2])
            elif seg.startswith("{") and seg.endswith("}"):
                self.segments.append(seg)
                self.names.append(seg[1:-1])
            elif "{" in seg or "}" in seg:
                raise ValueError(f"route template {template!r}: a variable must be a whole segment")
            else:
                self.segments.append(seg)
                self.literals += 1

    def shape(self) -> str:
        """The template with every variable as ``{}``, for comparisons."""
        out = ["{}" if s.startswith("{") else s for s in self.segments]
        if self.tail is not None:
            out.append("{}" if self.tail != "**" else "**")
        return "/" + "/".join(out)

    def match(self, path: str) -> dict[str, str] | None:
        """The captured variables when ``path`` matches, else None."""
        segs = path.strip("/").split("/") if path.strip("/") else []
        if self.tail is None:
            if len(segs) != len(self.segments):
                return None
            rest: list[str] = []
        else:
            if len(segs) < len(self.segments) + 1:
                return None
            rest = segs[len(self.segments) :]
            segs = segs[: len(self.segments)]
        out: dict[str, str] = {}
        for tpl, seg in zip(self.segments, segs, strict=True):
            if tpl.startswith("{"):
                if seg == "":
                    return None
                out[tpl[1:-1]] = seg
            elif tpl != seg:
                return None
        if self.tail is not None and self.tail != "**":
            if any(r == "" for r in rest):
                return None
            out[self.tail[1:-2]] = "/".join(rest)
        return out

    def __repr__(self) -> str:
        return f"PathTemplate({self.raw!r})"


def matching_templates(templates: Iterable[PathTemplate], path: str) -> list[tuple[PathTemplate, dict[str, str]]]:
    """Every matching template, most specific first: most literal segments,
    then an exact-length template before one with a tail, then by text."""
    found = [(t, v) for t in templates for v in (t.match(path),) if v is not None]
    found.sort(key=lambda tv: (-tv[0].literals, tv[0].tail is not None, tv[0].raw))
    return found


def best_templates(templates: Iterable[PathTemplate], path: str) -> list[tuple[PathTemplate, dict[str, str]]]:
    """The matching templates with the most literal segments, in a stable order."""
    found = matching_templates(templates, path)
    return [tv for tv in found if tv[0].literals == found[0][0].literals] if found else []


# What a route's body extractor returns: the fields to add to the path's, or
# None when the request carries no usable resource (a channel by name). The
# key "_action" names another of the integration's actions for this request
# (a reply in a thread rather than a top-level message); "_unresolved" gives
# the reason the resource could not be read, instead of returning None;
# "_effect" sets the request's effect (read, write, destructive) when the body
# decides it (a GraphQL query against a mutation on one endpoint); "_resource"
# sets the resource outright instead of filling the route's template.
Extractor = Callable[[Request, Mapping[str, str]], Mapping[str, str] | None]

# Where a connection's API lives: (host[:port], path prefix) pairs, from the
# connection's validated settings.
Locator = Callable[[Any], Sequence[tuple[str, str]]]


def host_key(host: str, port: int | None, scheme: str = "https") -> str:
    """``host`` or ``host:port`` when the port is not the scheme's default."""
    host = host.lower().strip("[]").rstrip(".")  # an IPv6 literal, with or without the URL's brackets; a fully qualified name's dot
    if port is None or (scheme == "https" and port == 443) or (scheme == "http" and port == 80):
        return host
    return f"{host}:{port}"


def endpoint_of_url(url: str) -> tuple[str, str] | None:
    """(host key, path prefix) of an https/http URL, or None."""
    u = urllib.parse.urlsplit(url)
    if u.scheme not in ("https", "http") or not u.hostname:
        return None
    return host_key(u.hostname, u.port, u.scheme), u.path.rstrip("/")


def _locate_by_url(settings: Any) -> Sequence[tuple[str, str]]:
    get = getattr(settings, "get", None)
    url = get("url") if callable(get) else ""
    ep = endpoint_of_url(url) if url else None
    return [ep] if ep is not None else []


@dataclass(frozen=True)
class Route:
    """One mapping from a request shape to an action.

    ``method`` is an HTTP method or ``*``. ``action`` is the integration's
    action name, or None for a request that needs no permission check (the
    agent's own identity, a rate-limit lookup): such a request is allowed
    and audited with its effect. ``resource`` is a template over the path's
    variables and the fields the ``body`` extractor adds. ``effect`` defaults
    from the method; ``untrusted_output`` defaults to True for reads.
    ``spec=False`` marks a catch-all that has no single operation in the
    vendor's API description."""

    method: str
    path: str
    action: str | None
    resource: str
    effect: str | None = None
    sends_out: bool = False
    untrusted_output: bool | None = None
    body: Extractor | None = None
    description: str = ""
    spec: bool = True

    def __post_init__(self) -> None:
        if self.effect is not None and self.effect not in EFFECTS:
            raise ValueError(f"route {self.method} {self.path}: effect must be one of {', '.join(EFFECTS)}")
        PathTemplate(self.path)  # validates the template

    def effect_for(self, method: str) -> str:
        return self.effect or effect_of_method(method)

    def untrusted_for(self, method: str) -> bool:
        if self.untrusted_output is not None:
            return self.untrusted_output
        return self.effect_for(method) == "read"


@dataclass(frozen=True)
class Observed:
    """What one request turned out to be."""

    action: str | None
    resource: str | None
    effect: str
    sends_out: bool
    untrusted_output: bool
    route: Route | None = None
    # Why the resource could not be built, when it could not.
    unresolved: str | None = None
    # The template's variables and the body fields, for the audit and the recorder.
    fields: Mapping[str, str] = field(default_factory=dict)


class RouteTable:
    """An integration's routes, with the hosts they apply to.

    ``prefixes`` are path prefixes to strip before matching (``/api/v3`` on
    GitHub Enterprise Server). ``unrouted`` lists ``(method, template)``
    shapes the table leaves out on purpose, with a reason each; a method
    ``*`` and ``**`` tails are allowed there. The spec-coverage tests hold
    the table to it: every write operation of the vendor's description under
    ``claims`` must be routed or listed."""

    def __init__(
        self,
        integration: str,
        routes: Sequence[Route],
        *,
        claims: Sequence[str] = (),
        unrouted: Mapping[tuple[str, str], str] | None = None,
        prefixes: Sequence[str] = (),
        locate: Locator | None = None,
    ) -> None:
        self.integration = integration
        self.routes = tuple(routes)
        self.claims = tuple(claims)
        self.unrouted = dict(unrouted or {})
        self.prefixes = tuple(p.rstrip("/") for p in prefixes if p.rstrip("/"))
        self._locate = locate or _locate_by_url
        self._templates: list[tuple[PathTemplate, Route]] = [(PathTemplate(r.path), r) for r in self.routes]
        self._unrouted = [(m.upper(), PathTemplate(t), reason) for (m, t), reason in self.unrouted.items()]
        seen: set[tuple[str, str]] = set()
        for r in self.routes:
            key = (r.method.upper(), PathTemplate(r.path).shape())
            if key in seen:
                raise ValueError(f"{integration}: route {r.method} {r.path} declared twice")
            seen.add(key)

    def endpoints(self, settings: Any) -> list[tuple[str, str]]:
        """The (host key, path prefix) pairs a connection's API answers on.
        By default the connection's ``url`` setting; integrations with a
        fixed host pass their own ``locate``."""
        return [(h.lower(), p.rstrip("/")) for h, p in self._locate(settings)]

    def strip(self, path: str, prefix: str = "") -> str:
        """``path`` without the connection's prefix (and the table's own)."""
        for p in (prefix.rstrip("/"), *self.prefixes):
            if p and (path == p or path.startswith(p + "/")):
                return path[len(p) :] or "/"
        return path

    def _encoded_prefix(self, path: str, prefix: str) -> bool:
        """True when the raw ``path`` reaches a prefix only once decoded
        (``/%761/...``, ``/v1%2F...``): the server strips it, so the rest
        would be checked as a different path than the one it serves."""
        decoded = urllib.parse.unquote(path)
        for p in (prefix.rstrip("/"), *self.prefixes):
            if p and (decoded == p or decoded.startswith(p + "/")) and not (path == p or path.startswith(p + "/")):
                return True
        return False

    def match(self, request: Request, prefix: str = "") -> Observed | None:
        """The observed action for a request, or None when no route matches.

        Matching runs on the raw (still percent-encoded) path, so an encoded
        ``/`` inside one segment stays inside it, as the server reads it;
        the captured variables are decoded afterwards."""
        method = request.method.upper()
        if self._encoded_prefix(request.raw_path, prefix):
            return Observed(None, None, effect_of_method(method), False, True, unresolved="the path spells its API prefix percent-encoded; it is not routed")
        if any(_decode_segment(seg) in (".", "..") for seg in request.raw_path.split("/")):
            # A server that collapses /a/b/.. acts on /a, a route reading /a/b/{x} would check something else.
            return Observed(None, None, effect_of_method(method), False, True, unresolved="the path has a . or .. segment; it is not routed")
        path = self.strip(request.raw_path, prefix)
        # The most specific template whose route takes this method: a GET on
        # /repos/{o}/{r}/contents/{path} falls through to /repos/{o}/{r}/{rest+}
        # when only PUT and DELETE are routed on the contents template.
        for tpl, raw_vars in matching_templates((t for t, _ in self._templates), path):
            for t, route in self._templates:
                if t is tpl and route.method.upper() in (method, "*"):
                    vars_ = {k: "/".join(_decode_segment(s) for s in v.split("/")) for k, v in raw_vars.items()}
                    return self._observe(route, method, request, vars_)
        return None

    def _observe(self, route: Route, method: str, request: Request, vars_: dict[str, str]) -> Observed:
        values: dict[str, str] = dict(vars_)
        unresolved: str | None = None
        action = route.action
        effect: str | None = None
        literal: str | None = None
        if route.body is not None:
            try:
                extra = route.body(request, values)
            except Exception as e:  # noqa: BLE001 - a broken extractor must not let the request through
                extra, unresolved = None, f"the request's resource could not be read ({type(e).__name__})"
            if extra is None:
                unresolved = unresolved or "the request does not name a resource toolpass can check"
            else:
                extra = dict(extra)
                if "_unresolved" in extra:
                    unresolved = extra.pop("_unresolved") or "the request does not name a resource toolpass can check"
                if "_action" in extra:
                    action = extra.pop("_action") or None
                if "_effect" in extra:
                    effect = extra.pop("_effect")
                    if effect not in EFFECTS:
                        effect, unresolved = None, unresolved or f"the request's effect {effect!r} is not one of {', '.join(EFFECTS)}"
                if "_resource" in extra:
                    literal = extra.pop("_resource")
                values.update(extra)
        resource: str | None = None
        if unresolved is None and literal is not None:
            resource = literal
        elif unresolved is None:
            resource = fill(route.resource, values)
            if resource is None:
                missing = [f for f in template_fields(route.resource) if f not in values]
                unresolved = f"the request does not carry {', '.join(missing)}"
        final = effect or route.effect_for(method)
        return Observed(
            action=action,
            resource=resource,
            effect=final,
            sends_out=route.sends_out,
            untrusted_output=route.untrusted_output if route.untrusted_output is not None else final == "read",
            route=route,
            unresolved=unresolved,
            fields=values,
        )

    def is_unrouted(self, method: str, path: str) -> str | None:
        """The reason a shape is left out on purpose, or None."""
        for m, tpl, reason in self._unrouted:
            if m in (method.upper(), "*") and tpl.match(path) is not None:
                return reason
        return None

    def __repr__(self) -> str:
        return f"RouteTable({self.integration!r}, {len(self.routes)} routes)"

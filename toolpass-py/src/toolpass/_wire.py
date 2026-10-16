"""The hook under the SDKs: every HTTP request a tool's body makes, checked
before it is sent.

``install()`` patches the HTTP client libraries present in the process at
one point each (urllib3's connection pool, the standard library's
``http.client``, httpx's transports, aiohttp's session) so that a request
made while a secured tool's body runs is seen here first. The request is
mapped to a connection of the configured permission engine by host, then to
an action by the integration's route table, and the action goes through the
toolkit's checks: scope, limits, the permission check of the session's
user, the untrusted-input and exfiltration guards. A failed check
raises ``ActionRefused`` (a ``ToolRefused``) from inside the client call,
so the request never leaves the process; the toolkit then reports the call
as refused even if the body swallowed the exception.

Outside a tool body nothing is checked: the permission engine's own
lookups, the framework's traffic and the application's requests pass
untouched.
"""

from __future__ import annotations

import asyncio
import contextvars
import dataclasses
import functools
import hashlib
import json
import logging
import sys
import threading
import urllib.parse
from collections.abc import Callable, Collection, Mapping, Sequence
from typing import TYPE_CHECKING, Any

from toolpass._context import CallContext, ObservedAction, current_call, in_agent_block
from toolpass._toolkit import ToolRefused
from toolpass.core.routes import Request, RouteTable, effect_of_method, host_key, rank

if TYPE_CHECKING:
    from toolpass._entitlements import ToolEntitlements

__all__ = ["ActionRefused", "Connections", "install", "installed", "observe"]

log = logging.getLogger("toolpass.toolkit")


class ActionRefused(ToolRefused):
    """A request the tool's body made was refused before it was sent.
    ``action`` is what the request turned out to be."""

    def __init__(self, tool: str, code: str, reason: str, action: ObservedAction) -> None:
        self.action = action
        super().__init__(tool, code, reason)


# -- connections ---------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class _Conn:
    id: str
    integration: str
    table: RouteTable
    endpoints: tuple[tuple[str, str], ...]  # (host key, path prefix)
    private: bool = False


class Connections:
    """The connections the hook can attribute requests to, by host."""

    def __init__(self, conns: Sequence[_Conn]) -> None:
        self._conns = tuple(conns)

    @classmethod
    def from_toolpass(cls, tp: Any, private: Collection[str] = ()) -> Connections:
        """From an in-process ``Toolpass``: every connection whose integration
        describes its routes. A remote ``Toolpass`` knows no connections;
        pass ``connections=`` to the toolkit then."""
        describe = getattr(tp, "_describe", None)
        if not callable(describe):
            return cls([])
        out: list[_Conn] = []
        for cid, integ, settings in describe():
            table = integ.routes()
            if table is None:
                continue
            out.append(_Conn(cid, integ.name(), table, tuple(table.endpoints(settings)), cid in private))
        return cls(out)

    @classmethod
    def from_mappings(cls, items: Sequence[Mapping[str, Any]], private: Collection[str] = ()) -> Connections:
        """From ``[{"id": ..., "integration": ..., "url": ...}, ...]``: the
        connection ids and URLs of a toolpass server's connections, for a
        toolkit whose ``permissions`` is remote."""
        from toolpass.core.integration import Settings
        from toolpass.integrations import registry

        reg = registry()
        out: list[_Conn] = []
        for item in items:
            cid, name = item.get("id"), item.get("integration")
            if not isinstance(cid, str) or not isinstance(name, str):
                raise ValueError("connections: each item needs a string 'id' and 'integration'")
            integ = reg.lookup(name)
            if integ is None:
                raise ValueError(f"connections: unknown integration {name!r} for {cid!r}")
            table = integ.routes()
            if table is None:
                continue
            values = {k: str(v) for k, v in item.items() if k not in ("id", "integration") and isinstance(v, (str, int))}
            for f in integ.fields():
                if f.name not in values and f.default:
                    values[f.name] = f.default
            settings = Settings(cid, name, values)
            out.append(_Conn(cid, name, table, tuple(table.endpoints(settings)), cid in private))
        return cls(out)

    def __len__(self) -> int:
        return len(self._conns)

    def resolve(self, request: Request) -> tuple[_Conn, str] | None:
        """The connection a request is addressed to and the path prefix to
        strip, or None. The longest matching prefix wins."""
        keys = {host_key(request.host, request.port, request.scheme), request.host.lower()}
        best: tuple[_Conn, str] | None = None
        for c in self._conns:
            for host, prefix in c.endpoints:
                if host not in keys:
                    continue
                if prefix and not (request.path == prefix or request.path.startswith(prefix + "/")):
                    continue
                if best is None or len(prefix) > len(best[1]):
                    best = (c, prefix)
        return best


# -- the check -----------------------------------------------------------------


def _key(r: Request) -> str:
    h = hashlib.blake2b(digest_size=8)
    h.update(r.body or b"")
    return f"{r.method} {r.scheme}://{r.host}:{r.port} {r.raw_path}?{r.query} {h.hexdigest()}"


def _refuse(ctx: CallContext, key: str, obs: ObservedAction, exc: ToolRefused) -> ToolRefused:
    obs.outcome = "refused"
    obs.code, obs.reason = exc.code, exc.reason
    with ctx.lock:
        ctx.observed.append(obs)
        ctx.refused[key] = exc
        if ctx.refusal is None:
            ctx.refusal = exc
    return exc


def observe(request: Request) -> None:
    """Check one outbound request for the call running in this context.
    Returns when it may be sent; raises ``ToolRefused`` when it may not."""
    ctx = current_call()
    if ctx is None:
        return  # not a tool body's request (or a thread that lost the context; see the docs)
    tool = ctx.spec.name
    key = _key(request)
    with ctx.lock:
        prior = ctx.refused.get(key)
    if prior is not None:
        raise prior  # a retry of a refused request gets the same answer, once recorded
    try:
        _check(ctx, request, key)
    except ToolRefused:
        raise
    except Exception as e:
        obs = ObservedAction(request.method, request.host, request.path, effect_of_method(request.method))
        exc = ToolRefused(tool, "check_error", f"the request {request.describe()} could not be checked ({type(e).__name__})")
        exc.__cause__ = e
        raise _refuse(ctx, key, obs, exc) from e


def _other_host(request: Request) -> str | None:
    """The Host header, when it names another host than the one the request
    is sent to; None when it is absent or the same."""
    given = (request.headers.get("host") or "").strip().lower()
    if not given:
        return None
    host, port = request.host.lower().strip("[]"), request.port
    same = {host, host_key(host, port, request.scheme)}
    if port is not None:
        same.add(f"{host}:{port}")
    bare = given.rstrip(".")
    if bare.startswith("["):  # an IPv6 literal, [::1] or [::1]:8443
        inner, _, rest = bare[1:].partition("]")
        bare = inner + rest
    return None if bare in same else given


def _check(ctx: CallContext, request: Request, key: str) -> None:
    tk, spec, session = ctx.toolkit, ctx.spec, ctx.session
    tool = spec.name
    ents: ToolEntitlements | None = ctx.entitlements
    obs = ObservedAction(request.method, request.host, request.path, effect_of_method(request.method))
    named = _other_host(request)
    if named is not None:
        # The server routes by the Host header (an S3 bucket, a virtual host), not by where the connection goes.
        raise _refuse(ctx, key, obs, ActionRefused(tool, "host_mismatch", f"{request.describe()}: its Host header names {named!r}, not {request.host}", obs))
    conns: Connections | None = tk._connections()
    hit = conns.resolve(request) if conns is not None else None

    if hit is None:
        if ents is not None and not ents.allows_host(host_key(request.host, request.port, request.scheme)) and not ents.allows_host(request.host):
            raise _refuse(
                ctx, key, obs, ActionRefused(tool, "unknown_host", f"{request.describe()}: {request.host} is not among this tool's recorded hosts", obs)
            )
        if ents is None and ctx.strict:
            raise _refuse(ctx, key, obs, ActionRefused(tool, "unknown_host", f"{request.describe()}: {request.host} is not a configured connection", obs))
        obs.note = "unknown_host"
        with ctx.lock:
            ctx.observed.append(obs)
        return

    conn, prefix = hit
    obs.connection, obs.integration = conn.id, conn.integration
    found = conn.table.match(request, prefix)
    if found is None:
        if obs.effect == "read":
            obs.note = "unmapped"
            obs.untrusted_output = True
            session._mark(untrusted=True, private=conn.private)
            with ctx.lock:
                ctx.observed.append(obs)
            return
        raise _refuse(
            ctx,
            key,
            obs,
            ActionRefused(tool, "unmapped_action", f"{request.describe()} is a {obs.effect} toolpass has no route for in {conn.integration}", obs),
        )
    obs.action, obs.resource, obs.effect = found.action, found.resource, found.effect
    obs.sends_out, obs.untrusted_output = found.sends_out, found.untrusted_output
    obs.fields = dict(found.fields)
    if found.unresolved is not None:
        raise _refuse(ctx, key, obs, ActionRefused(tool, "unresolved_resource", f"{request.describe()}: {found.unresolved}", obs))

    agent = in_agent_block()
    ent = None
    if ents is not None and found.action is not None:
        ent = ents.find(obs.name or "", found.resource, ctx.call.arguments)
        if ent is None:
            raise _refuse(
                ctx, key, obs, ActionRefused(tool, "unentitled_action", f"{obs.name} on {found.resource} is not among this tool's recorded actions", obs)
            )
        if ent.as_ == "agent":
            agent = True
    obs.as_agent = agent

    if spec.effect is not None and rank(found.effect) > rank(spec.effect):
        raise _refuse(
            ctx, key, obs, ActionRefused(tool, "effect_exceeded", f"{request.describe()} is a {found.effect} action; this tool is declared {spec.effect}", obs)
        )
    if spec.resources is not None and found.resource:
        typ, _, ident = found.resource.partition(":")
        try:
            spec.resources.check({typ: ident})
        except Exception as e:  # noqa: BLE001 - an out-of-scope resource, or a rule that could not decide
            raise _refuse(ctx, key, obs, ActionRefused(tool, "out_of_scope", f"{found.resource} is outside this tool's scope ({e})", obs)) from None

    taken: list[tuple[str, int]] = []
    if spec.effect is None and ents is None and found.effect in tk.limits:
        taken = [(f"effect:{found.effect}", tk.limits[found.effect])]
        exhausted = session._reserve(taken)
        if exhausted is not None:
            raise _refuse(ctx, key, obs, ActionRefused(tool, "limit_reached", f"this session reached its limit for {exhausted}", obs))
    try:
        _authorize(ctx, request, key, obs, conn, agent, ent)
    except ToolRefused:
        if taken:
            session._release(taken)
        raise
    session._mark(untrusted=found.untrusted_output, private=conn.private)
    with ctx.lock:
        ctx.observed.append(obs)


def _authorize(ctx: CallContext, request: Request, key: str, obs: ObservedAction, conn: _Conn, agent: bool, ent: Any) -> None:
    tk, spec, session = ctx.toolkit, ctx.spec, ctx.session
    tool = spec.name
    if obs.action is not None and obs.resource is not None and not agent and (obs.name, obs.resource) not in ctx.preallowed:
        tp = tk.permissions
        if tp is None:
            raise _refuse(
                ctx,
                key,
                obs,
                ActionRefused(
                    tool, "authorization_error", f"{obs.name} on {obs.resource} needs a permission check and the toolkit has no permissions engine", obs
                ),
            )
        groups = None if session.groups is None else list(session.groups)
        try:
            d = tp.check(session.user, conn.id, obs.action, obs.resource, groups)
            allowed, reason = getattr(d, "allowed", False) is True, str(getattr(d, "reason", ""))
        except Exception as e:  # noqa: BLE001 - a failed lookup is a refusal
            raise _refuse(ctx, key, obs, ActionRefused(tool, "authorization_error", f"authorization could not be decided ({type(e).__name__})", obs)) from None
        if not allowed:
            raise _refuse(
                ctx,
                key,
                obs,
                ActionRefused(tool, "not_authorized", f"{session.user} may not {obs.action} on {obs.resource} in {conn.id}: {reason or 'denied'}", obs),
            )

    if (spec.check_untrusted or obs.effect != "read") and session._carries_untrusted(request.text()):
        message = f"the request {request.describe()} repeats content from an untrusted source seen in this session, so it may have been injected"
        raise _refuse(ctx, key, obs, ActionRefused(tool, "untrusted_input", message, obs))
    if obs.sends_out and tk.exfiltration != "off" and session.read_private and session.saw_untrusted:
        message = f"this session has read private data and seen untrusted content, and {request.describe()} sends data out"
        raise _refuse(ctx, key, obs, ActionRefused(tool, "exfiltration_risk", message, obs))


# -- the patches ---------------------------------------------------------------

# True while a request is already being checked or sent by an outer hook, so
# a lower-level hook (urllib3 v1 reaching http.client) lets it through, and
# so the permission engine's own lookups from inside a check are not checked.
_inner: contextvars.ContextVar[bool] = contextvars.ContextVar("toolpass_wire_inner", default=False)

_installed: set[str] = set()
_install_lock = threading.Lock()

_KNOWN = ("urllib3", "snowflake.connector.vendored.urllib3", "http.client", "httpx", "aiohttp")


def installed() -> frozenset[str]:
    """The client libraries hooked so far."""
    return frozenset(_installed)


def install() -> None:
    """Hook every supported client library that is imported. Idempotent;
    call it again after importing another library, or let the toolkit do it
    on the next tool call."""
    with _install_lock:
        for name in _KNOWN:
            if name in _installed or name not in sys.modules:
                continue
            try:
                _PATCHERS[name]()
            except Exception as e:  # noqa: BLE001 - a library whose shape changed is logged, not fatal
                log.warning("toolpass: could not hook %s (%s); its requests are not checked", name, type(e).__name__)
            _installed.add(name)


def _headers(h: Any) -> dict[str, str]:
    out: dict[str, str] = {}
    if h is None:
        return out
    items = h.items() if hasattr(h, "items") else h
    for k, v in items:
        if isinstance(k, bytes):
            k = k.decode("latin-1")
        if isinstance(v, bytes):
            v = v.decode("latin-1")
        out[str(k).lower()] = str(v)
    return out


def _bytes(body: Any) -> bytes | None:
    if body is None:
        return None
    if isinstance(body, (bytes, bytearray, memoryview)):
        return bytes(body)
    if isinstance(body, str):
        return body.encode("utf-8", "replace")
    return None  # a file or an iterator is not read


def _split(url: str, default_scheme: str, default_host: str, default_port: int | None) -> tuple[str, str, int | None, str, str]:
    """(scheme, host, port, raw path, query) of a request-target that may be
    origin-form (/path?q) or absolute-form (http://host/path, through a proxy)."""
    if url.startswith(("http://", "https://")):
        u = urllib.parse.urlsplit(url)
        return u.scheme, u.hostname or default_host, u.port, u.path or "/", u.query
    path, _, query = url.partition("?")
    return default_scheme, default_host, default_port, path or "/", query


def _guarded(fn: Callable[..., Any], request: Request) -> Any:
    """Check, then call ``fn`` with the hook marked as already applied."""
    token = _inner.set(True)
    try:
        observe(request)
        return fn()
    finally:
        _inner.reset(token)


def _observe_inner(request: Request) -> None:
    """``observe`` with the hook marked as applied: the permission engine's
    own lookups made from inside the check are not checked themselves."""
    token = _inner.set(True)
    try:
        observe(request)
    finally:
        _inner.reset(token)


def _patch_urllib3_module(mod: Any) -> None:
    pool_cls = mod.connectionpool.HTTPConnectionPool
    original = pool_cls.urlopen
    if getattr(original, "__toolpass__", False):
        return

    @functools.wraps(original)
    def urlopen(self: Any, method: str, url: str, body: Any = None, headers: Any = None, *args: Any, **kwargs: Any) -> Any:
        if _inner.get() or current_call() is None:
            return original(self, method, url, body, headers, *args, **kwargs)
        scheme, host, port, path, query = _split(url, self.scheme, self.host, self.port)
        req = Request(
            scheme, host, port, method.upper(), path, query, _headers(headers if headers is not None else getattr(self, "headers", None)), _bytes(body)
        )
        return _guarded(lambda: original(self, method, url, body, headers, *args, **kwargs), req)

    urlopen.__toolpass__ = True  # type: ignore[attr-defined]
    pool_cls.urlopen = urlopen


def _patch_urllib3() -> None:
    _patch_urllib3_module(sys.modules["urllib3"])


def _patch_snowflake_urllib3() -> None:
    _patch_urllib3_module(sys.modules["snowflake.connector.vendored.urllib3"])


def _patch_http_client() -> None:
    import http.client as hc

    original = hc.HTTPConnection.request
    if getattr(original, "__toolpass__", False):
        return

    @functools.wraps(original)
    def request(self: Any, method: str, url: str, body: Any = None, headers: Any = None, **kwargs: Any) -> Any:
        hdrs = headers if headers is not None else {}
        if _inner.get() or current_call() is None:
            return original(self, method, url, body, hdrs, **kwargs)
        scheme = "https" if isinstance(self, hc.HTTPSConnection) else "http"
        host = getattr(self, "_tunnel_host", None) or self.host
        port = getattr(self, "_tunnel_port", None) or self.port
        scheme, host, port, path, query = _split(url, scheme, host, port)
        req = Request(scheme, host, port, method.upper(), path, query, _headers(hdrs), _bytes(body))
        return _guarded(lambda: original(self, method, url, body, hdrs, **kwargs), req)

    request.__toolpass__ = True  # type: ignore[attr-defined]
    hc.HTTPConnection.request = request  # type: ignore[method-assign]


def _httpx_request(r: Any) -> Request:
    url = r.url
    raw = url.raw_path.decode("ascii", "replace") if isinstance(url.raw_path, bytes) else str(url.raw_path)
    path, _, query = raw.partition("?")
    try:
        body = bytes(r.content)
    except Exception:  # noqa: BLE001 - a streamed body is not read
        body = None
    return Request(url.scheme, url.host, url.port, r.method.upper(), path or "/", query, _headers(r.headers), body)


def _patch_httpx() -> None:
    httpx = sys.modules["httpx"]
    sync_cls, async_cls = httpx.HTTPTransport, httpx.AsyncHTTPTransport
    sync_original, async_original = sync_cls.handle_request, async_cls.handle_async_request
    if getattr(sync_original, "__toolpass__", False):
        return

    @functools.wraps(sync_original)
    def handle_request(self: Any, request: Any) -> Any:
        if _inner.get() or current_call() is None:
            return sync_original(self, request)
        return _guarded(lambda: sync_original(self, request), _httpx_request(request))

    @functools.wraps(async_original)
    async def handle_async_request(self: Any, request: Any) -> Any:
        if _inner.get() or current_call() is None:
            return await async_original(self, request)
        await asyncio.to_thread(_observe_inner, _httpx_request(request))  # the permission check is blocking I/O
        token = _inner.set(True)
        try:
            return await async_original(self, request)
        finally:
            _inner.reset(token)

    handle_request.__toolpass__ = True  # type: ignore[attr-defined]
    handle_async_request.__toolpass__ = True  # type: ignore[attr-defined]
    sync_cls.handle_request = handle_request
    async_cls.handle_async_request = handle_async_request


def _patch_aiohttp() -> None:
    aiohttp = sys.modules["aiohttp"]
    cls = aiohttp.ClientSession
    original = cls._request
    if getattr(original, "__toolpass__", False):
        return

    @functools.wraps(original)
    async def _request(self: Any, method: str, str_or_url: Any, **kwargs: Any) -> Any:
        if _inner.get() or current_call() is None:
            return await original(self, method, str_or_url, **kwargs)
        url = self._build_url(str_or_url) if hasattr(self, "_build_url") else str_or_url
        body: bytes | None
        headers = _headers(kwargs.get("headers"))
        if kwargs.get("json") is not None:
            body = json.dumps(kwargs["json"]).encode()
            headers.setdefault("content-type", "application/json")
        else:
            data = kwargs.get("data")
            if isinstance(data, Mapping):
                body = urllib.parse.urlencode({k: v for k, v in data.items() if isinstance(v, (str, int, float))}).encode()
                headers.setdefault("content-type", "application/x-www-form-urlencoded")
            else:
                body = _bytes(data)
        req = Request(str(url.scheme), str(url.host or ""), url.port, method.upper(), url.raw_path or "/", url.raw_query_string or "", headers, body)
        await asyncio.to_thread(_observe_inner, req)
        token = _inner.set(True)
        try:
            return await original(self, method, str_or_url, **kwargs)
        finally:
            _inner.reset(token)

    _request.__toolpass__ = True  # type: ignore[attr-defined]
    cls._request = _request


_PATCHERS: dict[str, Callable[[], None]] = {
    "urllib3": _patch_urllib3,
    "snowflake.connector.vendored.urllib3": _patch_snowflake_urllib3,
    "http.client": _patch_http_client,
    "httpx": _patch_httpx,
    "aiohttp": _patch_aiohttp,
}

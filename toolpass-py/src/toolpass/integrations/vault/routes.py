"""Vault HTTP API requests (``/v1/<path>``), mapped to the capability Vault
itself checks on the path.

Vault authorizes a request by its path and operation alone, whatever the
mount behind it, so one shape covers every engine and every mount name: the
method decides the capability (for a write, with the path; see below) and the
resource is ``path:<the request path>`` with the request's namespace beside it
(``?namespace=`` for the root, or the ``X-Vault-Namespace`` header), which the
check resolves against the connection's namespace.

    GET (with ?list=true: list), HEAD   raw:read or raw:list   read
    LIST                                raw:list               read
    POST, PUT                           secret.write           write (create and update: which one Vault
                                                               checks depends on whether the path exists)
    POST, PUT on a fixed-mount endpoint raw:update             write
      without an existence check
    PATCH                               raw:patch              write
    DELETE                              raw:delete             destructive

Refused, at any depth of the path (a leading part may be a namespace):
paths Vault scopes to the calling token, which is the agent's and not the
user's (``cubbyhole/``, ``auth/token/*-self``, a token's MFA or OIDC token,
control-group approval); token creation, whose new token inherits the
agent's policies; paths that need ``sudo`` besides the capability (Vault's
root-protected paths, a PKI root); operator operations Vault does not
authorize by policy (unseal, rekey, root generation, replication, license).
A path toolpass cannot check exactly (an empty, dot-only or unusual segment,
a trailing slash on anything but a list, an unreadable query) is refused too.

Vault checks create on a POST or PUT only where the endpoint has an existence
check (``x-vault-createSupported`` in its API description) and the target does
not exist yet, and update everywhere else. Which endpoint a path reaches is
known only in the mounts Vault itself fixes, ``sys/``, ``identity/`` and
``auth/token/`` (relative to the request's namespace, since a leading segment of
the path may be a namespace or a mount): there the endpoints described without
an existence check need update alone. Any other path may be in a mount an
operator named, KV v1 among them, where every path has one, so it needs both
(as ``transit/encrypt`` does in any case: it creates a missing key).

The agent's own token (``lookup-self``, ``capabilities-self``, its resultant
ACL) and the unauthenticated health endpoints need no user permission.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from toolpass.core.catalog import ResourceError, parse_query
from toolpass.core.routes import Extractor, Request, Route, RouteTable, endpoint_of_url
from toolpass.integrations.vault.actions import valid_namespace, valid_path

__all__ = ["ROUTES"]

_AGENT = "acts on the calling token or its identity, which is the agent's, not the user's"
_TOKEN = "creates a token that inherits the agent's policies (Vault checks them against the calling token)"
_SUDO = "needs sudo besides the operation's capability, and toolpass checks one capability per request"
_OPERATOR = "an operator operation Vault does not authorize by policy"

_WRITES = frozenset({"POST", "PUT", "PATCH", "DELETE"})
# What the extractor writes, for the listing.
_PATH = "path:{path}?namespace={namespace}"

# Vault-style globs on the path relative to its namespace, with the trailing
# slash a list adds: "+" is one segment, a trailing "*" any suffix. Each with
# the methods it refuses (None: every method) and why.
_REFUSED: tuple[tuple[str, frozenset[str] | None, str], ...] = (
    # The calling token's own storage and state.
    ("cubbyhole", None, _AGENT),
    ("cubbyhole/*", None, _AGENT),
    ("auth/token/renew-self", None, _AGENT),
    ("auth/token/revoke-self", None, _AGENT),
    ("identity/mfa/method/totp/generate", None, _AGENT),
    ("identity/mfa/method/totp/destroy", None, _AGENT),
    ("sys/mfa/method/totp/+/generate", None, _AGENT),
    ("identity/oidc/token/*", None, _AGENT),
    ("identity/oidc/provider/+/authorize", None, _AGENT),
    ("sys/control-group/authorize", None, _AGENT),
    # Tokens minted with the agent's policies.
    ("auth/token/create", None, _TOKEN),
    ("auth/token/create/*", None, _TOKEN),
    ("auth/token/create-orphan", None, _TOKEN),
    # Root-protected paths of the token store and the system backend.
    ("auth/token/revoke-orphan", None, _SUDO),
    ("auth/token/revoke-orphan/*", None, _SUDO),
    ("auth/token/accessors*", None, _SUDO),
    ("sys/auth/*", None, _SUDO),
    ("sys/remount", None, _SUDO),
    ("sys/audit", None, _SUDO),
    ("sys/audit/*", None, _SUDO),
    ("sys/raw", None, _SUDO),
    ("sys/raw/*", None, _SUDO),
    ("sys/rotate", None, _SUDO),
    ("sys/config/cors", None, _SUDO),
    ("sys/config/auditing/*", None, _SUDO),
    ("sys/config/ui/headers/*", None, _SUDO),
    ("sys/plugins/catalog/*", None, _SUDO),
    ("sys/plugins/runtimes/catalog/*", None, _SUDO),
    ("sys/revoke-prefix/*", None, _SUDO),
    ("sys/revoke-force/*", None, _SUDO),
    ("sys/leases", None, _SUDO),
    ("sys/leases/revoke-prefix/*", None, _SUDO),
    ("sys/leases/revoke-force/*", None, _SUDO),
    ("sys/leases/lookup/*", None, _SUDO),
    ("sys/storage/raft/snapshot-auto/config/*", None, _SUDO),
    ("sys/internal/inspect/*", None, _SUDO),
    ("sys/seal", None, _SUDO),
    ("sys/step-down", None, _SUDO),
    # Operator operations: key shares, replication, the license.
    ("sys/init", None, _OPERATOR),
    ("sys/unseal", None, _OPERATOR),
    ("sys/rekey/*", None, _OPERATOR),
    ("sys/rekey-recovery-key/*", None, _OPERATOR),
    ("sys/generate-root", None, _OPERATOR),
    ("sys/generate-root/*", None, _OPERATOR),
    ("sys/generate-recovery-token/*", None, _OPERATOR),
    ("sys/replication/*", _WRITES, _OPERATOR),
    ("sys/license/*", _WRITES, _OPERATOR),
)


# The POST and PUT endpoints of Vault's fixed mounts that Vault's API
# description lists without an existence check (not x-vault-createSupported),
# as globs on the path relative to its namespace: a write there is an update
# whether or not its target exists. Refused paths are left out; a trailing "*"
# stands for a parameter that may span segments (a mount, a lease ID).
# Everything else needs create and update. Not here, since they have one:
# auth/token/roles/+, identity/oidc/{assignment,client,key,provider,role,scope}/+,
# sys/managed-keys/+/+ and its test/sign, sys/raw/*.
_UPDATE_ONLY: tuple[str, ...] = (
    "auth/token/lookup",
    "auth/token/lookup-accessor",
    "auth/token/lookup-self",
    "auth/token/renew",
    "auth/token/renew-accessor",
    "auth/token/revoke",
    "auth/token/revoke-accessor",
    "auth/token/tidy",
    "identity/alias",
    "identity/alias/id/+",
    "identity/entity",
    "identity/entity/batch-delete",
    "identity/entity/id/+",
    "identity/entity/merge",
    "identity/entity/name/+",
    "identity/entity-alias",
    "identity/entity-alias/id/+",
    "identity/group",
    "identity/group/id/+",
    "identity/group/name/+",
    "identity/group-alias",
    "identity/group-alias/id/+",
    "identity/lookup/entity",
    "identity/lookup/group",
    "identity/mfa/login-enforcement/+",
    "identity/mfa/method/duo",
    "identity/mfa/method/duo/+",
    "identity/mfa/method/okta",
    "identity/mfa/method/okta/+",
    "identity/mfa/method/pingid",
    "identity/mfa/method/pingid/+",
    "identity/mfa/method/totp",
    "identity/mfa/method/totp/+",
    "identity/oidc/config",
    "identity/oidc/introspect",
    "identity/oidc/key/+/rotate",
    "identity/oidc/provider/+/token",
    "identity/oidc/provider/+/userinfo",
    "identity/persona",
    "identity/persona/id/+",
    "sys/audit-hash/*",
    "sys/capabilities",
    "sys/capabilities-accessor",
    "sys/capabilities-self",
    "sys/config/control-group",
    "sys/config/group-policy-application",
    "sys/config/reload/+",
    "sys/control-group/request",
    "sys/decode-token",
    "sys/internal/counters/config",
    "sys/internal/specs/openapi",
    "sys/leases/lookup",
    "sys/leases/renew",
    "sys/leases/renew/*",
    "sys/leases/revoke",
    "sys/leases/revoke/*",
    "sys/leases/tidy",
    "sys/locked-users/+/unlock/+",
    "sys/loggers",
    "sys/loggers/+",
    "sys/mfa/method/duo/+",
    "sys/mfa/method/okta/+",
    "sys/mfa/method/pingid/+",
    "sys/mfa/method/totp/+",
    "sys/mfa/method/totp/+/admin-destroy",
    "sys/mfa/method/totp/+/admin-generate",
    "sys/mfa/validate",
    "sys/mounts/*",
    "sys/namespaces/*",
    "sys/plugins/reload/backend",
    "sys/policies/acl/+",
    "sys/policies/egp/+",
    "sys/policies/password/+",
    "sys/policies/rgp/+",
    "sys/policy/+",
    "sys/quotas/config",
    "sys/quotas/lease-count/+",
    "sys/quotas/rate-limit/+",
    "sys/renew",
    "sys/renew/*",
    "sys/revoke",
    "sys/revoke/*",
    "sys/rotate/config",
    "sys/sealwrap/rewrap",
    "sys/tools/hash",
    "sys/tools/hash/+",
    "sys/tools/random",
    "sys/tools/random/+",
    "sys/tools/random/+/+",
    "sys/wrapping/lookup",
    "sys/wrapping/rewrap",
    "sys/wrapping/unwrap",
    "sys/wrapping/wrap",
)


def _glob(pattern: str) -> re.Pattern[str]:
    tail = pattern.endswith("*")
    body = pattern[:-1] if tail else pattern
    rx = "/".join("[^/]+" if seg == "+" else re.escape(seg) for seg in body.split("/"))
    return re.compile(rx + (".*" if tail else ""), re.DOTALL)


_REFUSED_RE = tuple((_glob(p), methods, reason) for p, methods, reason in _REFUSED)
_UPDATE_ONLY_RE = tuple(_glob(p) for p in _UPDATE_ONLY)

# Go's strconv.ParseBool, which Vault applies to ?list=.
_TRUE = frozenset({"1", "t", "T", "TRUE", "true", "True"})
_FALSE = frozenset({"0", "f", "F", "FALSE", "false", "False"})


def _refusal(method: str, segs: Sequence[str], listing: bool) -> str | None:
    """Why a path is refused, or None. Every suffix is tried: a leading part
    of the path may be a namespace (``team/cubbyhole/x``)."""
    if method == "DELETE" and segs[-1] == "root":
        return "deleting a PKI root " + _SUDO  # pki/root is root-protected; a key named root is refused with it
    if method in ("POST", "PUT") and len(segs) >= 2 and segs[-2:] == ["root", "sign-self-issued"]:
        return "signing with a PKI root " + _SUDO
    for i in range(len(segs)):
        rest = "/".join(segs[i:]) + ("/" if listing else "")
        for rx, methods, reason in _REFUSED_RE:
            if (methods is None or method in methods) and rx.fullmatch(rest):
                return f"{'/'.join(segs[i:])}: {reason}"
    return None


def _namespace(r: Request) -> str | None:
    """The request's namespace from X-Vault-Namespace, as Vault canonicalizes
    it (one leading slash dropped); "" for none, None when it is not one
    toolpass reads."""
    raw = r.headers.get("x-vault-namespace", "")
    if raw == "":
        return ""
    ns = raw.removeprefix("/").removesuffix("/")
    return ns if ns != "" and valid_namespace(ns) else None


def _listing(r: Request, method: str) -> bool | str:
    """Whether a GET is a list (``?list=true``), or why it cannot be told."""
    if method == "LIST":
        return True
    if method not in ("GET", "HEAD") or not r.query:
        return False
    try:
        q = parse_query(r.query)  # Go's url.ParseQuery, which Vault reads the query with
    except ResourceError:
        return "the query cannot be read exactly as Vault reads it"
    values = q.get("list", [])
    if len(values) > 1:
        return "list is given more than once"
    if not values or values[0] == "":
        return False
    if values[0] in _TRUE:
        return True
    if values[0] in _FALSE:
        return False
    return f"list={values[0]!r} is not a boolean Vault accepts"


def _capability(r: Request, vars_: Mapping[str, str]) -> dict[str, str]:
    method = r.method.upper()
    path = vars_.get("path", "")
    if "//" in r.raw_path:
        return {"_unresolved": "the path has an empty segment"}
    if not valid_path(path):
        return {"_unresolved": f"{path!r} is not a Vault path toolpass checks (plain segments of letters, digits and _ . - @ : ~ =)"}
    listing = _listing(r, method)
    if isinstance(listing, str):
        return {"_unresolved": listing}
    if r.raw_path.endswith("/") and not listing:
        return {"_unresolved": "a trailing slash is a different Vault path than the one toolpass checks; only a list may end in one"}
    ns = _namespace(r)
    if ns is None:
        return {"_unresolved": "the X-Vault-Namespace header is not a namespace path toolpass reads"}
    why = _refusal(method, path.split("/"), listing)
    if why is not None:
        return {"_unresolved": f"not routed: {why}"}
    if listing:
        action, effect = "raw:list", "read"
    else:
        action, effect = {
            "GET": ("raw:read", "read"),
            "HEAD": ("raw:read", "read"),
            "POST": ("secret.write", "write"),
            "PUT": ("secret.write", "write"),
            "PATCH": ("raw:patch", "write"),
            "DELETE": ("raw:delete", "destructive"),
        }[method]
        if action == "secret.write" and any(rx.fullmatch(path) for rx in _UPDATE_ONLY_RE):
            # No existence check behind the path: Vault checks update alone.
            action = "raw:update"
        if effect == "write" and any(s in ("delete", "destroy") or s.startswith("revoke") for s in path.split("/")):
            # KV v2's delete/ and destroy/, revocations: writes that remove something.
            effect = "destructive"
    return {"_action": action, "_effect": effect, "_resource": f"path:{path}?namespace={ns}"}


def _routes() -> list[Route]:
    cap: Extractor = _capability
    out = [
        Route("GET", "/{path+}", "raw:read", _PATH, body=cap, spec=False, description="read; ?list=true is list"),
        Route("HEAD", "/{path+}", "raw:read", _PATH, body=cap, spec=False),
        Route("LIST", "/{path+}", "raw:list", _PATH, effect="read", body=cap, spec=False),
        Route(
            "POST",
            "/{path+}",
            "secret.write",
            _PATH,
            body=cap,
            spec=False,
            description="create and update; update alone on sys/, identity/, auth/token/ endpoints without an existence check",
        ),
        Route(
            "PUT",
            "/{path+}",
            "secret.write",
            _PATH,
            body=cap,
            spec=False,
            description="create and update; update alone on sys/, identity/, auth/token/ endpoints without an existence check",
        ),
        Route("PATCH", "/{path+}", "raw:patch", _PATH, body=cap, spec=False),
        Route("DELETE", "/{path+}", "raw:delete", _PATH, body=cap, spec=False),
        # The agent's own token, and health that needs no token at all.
        Route("GET", "/auth/token/lookup-self", None, "", untrusted_output=False, description="the agent's own token"),
        Route("POST", "/auth/token/lookup-self", None, "", effect="read", untrusted_output=False, description="the agent's own token"),
        Route("POST", "/sys/capabilities-self", None, "", effect="read", untrusted_output=False, description="the agent's own capabilities"),
        Route("GET", "/sys/internal/ui/resultant-acl", None, "", untrusted_output=False, description="the agent's own policies"),
        Route("GET", "/sys/health", None, "", untrusted_output=False),
        Route("GET", "/sys/seal-status", None, "", untrusted_output=False),
        Route("GET", "/sys/leader", None, "", untrusted_output=False),
    ]
    return out


def _locate(settings: Any) -> Sequence[tuple[str, str]]:
    """<url>/v1: every Vault API path is under it."""
    url = settings.get("url") if hasattr(settings, "get") else ""
    ep = endpoint_of_url(url) if url else None
    return [(ep[0], ep[1] + "/v1")] if ep is not None else []


ROUTES = RouteTable("vault", _routes(), claims=("/",), locate=_locate)

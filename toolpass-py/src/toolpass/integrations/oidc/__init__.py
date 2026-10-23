"""The oidc integration: a user's groups from their own ID token, for any
OpenID Connect provider.

A system that grants by group (Argo CD, Kubernetes, an aws role map) reads
the groups from the token the user logged in with. toolpass never sees that
login, so this connection takes the token from the check itself (the
request's `token`), verifies it the way the system did (the provider's
published signing keys, issuer, audience, expiry) and reads the groups
claim. That works with every provider that speaks OIDC discovery, with no
provider API and no credential, and sees exactly the groups the system saw,
including any filtering the provider applies to the claim.

It answers no actions of its own: it serves only as a groups_connection.
"""

from __future__ import annotations

from typing import Any

from toolpass.authx import jwt
from toolpass.authx.util import go_json_loads
from toolpass.core import jsonx
from toolpass.core.cache import TTL
from toolpass.core.catalog import Action
from toolpass.core.context import Context
from toolpass.core.decision import Code, Decision, ToolpassError, errorf, wrap_error
from toolpass.core.errors import go_quote, go_trim_space
from toolpass.core.integration import (
    CAP_GROUPS,
    CheckRequest,
    Connection,
    Deps,
    Field,
    Identity,
    Integration,
    ProbeResult,
    Settings,
    User,
    validate_https_url,
)
from toolpass.net import httpx

__all__ = ["CLOCK_SKEW", "INTEGRATION", "KEYS_RETRY", "KEYS_TTL", "OIDC", "OIDCConnection"]

# How long the discovery document and the signing keys are reused.
KEYS_TTL = 3600.0
# How soon after a fetch an unknown kid may fetch the keys again (a key
# rollover), so a flood of bad tokens cannot flood the provider.
KEYS_RETRY = 60.0
# Tolerance on exp and nbf, as providers and systems usually allow.
CLOCK_SKEW = 60.0
DISCOVERY_PATH = "/.well-known/openid-configuration"


def _validate_issuer(v: str) -> None:
    validate_https_url(v)


def _validate_claim(v: str) -> None:
    if v == "" or any(c.isspace() for c in v):
        raise ValueError("a claim name must not be empty or contain whitespace")


class OIDC(Integration):
    """The oidc product."""

    def name(self) -> str:
        return "oidc"

    def provides(self) -> frozenset[str]:
        return frozenset({CAP_GROUPS})

    def fields(self) -> list[Field]:
        return [
            Field(
                name="issuer",
                required=True,
                validate=_validate_issuer,
                description="the provider's issuer URL, as in the system's OIDC settings, e.g. https://acme.okta.com/oauth2/default",
            ),
            Field(
                name="audience",
                required=True,
                description="the client id the system logs in with: the token's aud must carry it",
            ),
            Field(name="groups_claim", default="groups", validate=_validate_claim, description="the claim holding the user's groups"),
            Field(
                name="email_claim",
                default="email",
                validate=_validate_claim,
                description="the claim holding the user's email, which must equal the check's user",
            ),
        ]

    def actions(self) -> list[Action]:
        return []

    def new(self, ctx: Context, s: Settings, d: Deps) -> Connection:
        issuer = s.get("issuer").rstrip("/")
        c = OIDCConnection(
            issuer=issuer,
            audience=s.get("audience"),
            groups_claim=s.get("groups_claim") or "groups",
            email_claim=s.get("email_claim") or "email",
            http=httpx.Client(http=d.http_client(s), base=issuer, logger=d.logger),
            now=d.now,
        )
        return c


class OIDCConnection(Connection):
    """One identity provider."""

    # The provider's own claim limits are not known here; a token over them
    # carries no groups claim, which reads as a user in no group.
    groups_claim_limit = None

    def __init__(self, issuer: str, audience: str, groups_claim: str, email_claim: str, http: httpx.Client, now: Any) -> None:
        self.issuer = issuer
        self.audience = audience
        self.groups_claim = groups_claim
        self.email_claim = email_claim
        self.http = http
        self.now = now
        # The discovery document under "" and the keys by kid under their
        # jwks_uri; one entry each.
        self.discovery: TTL[str, dict[str, Any]] = TTL(1)
        self.discovery.set_clock(now)
        self.keys: TTL[str, dict[str, Any]] = TTL(1)
        self.keys.set_clock(now)
        self.keys_fetched = 0.0

    def resolve_identity(self, ctx: Context, u: User) -> Identity:
        """The token's subject, display email and groups."""
        claims = self._verify(ctx, u)
        return Identity(id=str(claims.get("sub", "")), display=u.email, groups=self._groups_of(claims))

    def check(self, ctx: Context, r: CheckRequest) -> Decision:
        raise errorf(Code.INVALID_REQUEST, "an oidc connection names a user's groups for other connections (groups_connection); it answers no actions")

    def user_groups(self, ctx: Context, user: User) -> tuple[str, ...]:
        """The groups claim of the user's verified token."""
        return self._groups_of(self._verify(ctx, user))

    def _groups_of(self, claims: dict[str, Any]) -> tuple[str, ...]:
        raw = claims.get(self.groups_claim)
        if raw is None:
            # A distributed claim (OIDC Core 5.6.2): the provider names the
            # claim in _claim_names and leaves it for the system to fetch
            # (Entra ID's "overage" above its group cap). What the system
            # saw is unknown here.
            names = claims.get("_claim_names")
            if isinstance(names, dict) and self.groups_claim in names:
                raise errorf(
                    Code.UNSUPPORTED, f"the token carries its {self.groups_claim} claim by reference (an overage); which groups the system saw is unknown"
                )
            return ()
        if not isinstance(raw, list) or not all(isinstance(g, str) for g in raw):
            raise errorf(Code.UNSUPPORTED, f"the token's {self.groups_claim} claim is not a list of strings")
        out: list[str] = []
        for g in raw:
            if g != "" and g not in out:
                out.append(g)
        return tuple(out)

    # -- verification --

    def _verify(self, ctx: Context, u: User) -> dict[str, Any]:
        """The claims of u.token once its signature, issuer, audience,
        expiry and email are checked. A token that fails is
        INVALID_REQUEST: nothing about the user is known from it."""
        token = u.token
        if token == "":
            raise errorf(Code.INVALID_REQUEST, "no token: an oidc groups_connection reads the user's groups from the ID token sent in the check's token field")
        parts = token.split(".")
        if len(parts) != 3:
            raise errorf(Code.INVALID_REQUEST, "token is not a compact JWS (header.payload.signature)")
        try:
            header = jsonx.obj(go_json_loads(jwt.b64url_decode(parts[0])))
            claims = jsonx.obj(go_json_loads(jwt.b64url_decode(parts[1])))
            sig = jwt.b64url_decode(parts[2])
        except (ValueError, RecursionError) as e:
            raise wrap_error(Code.INVALID_REQUEST, e, "token is not a readable JWS") from e
        alg = header.get("alg")
        if not isinstance(alg, str) or alg not in jwt.ASYMMETRIC_ALGS:
            raise errorf(Code.INVALID_REQUEST, f"token algorithm {go_quote(str(alg))} is not one a provider signs ID tokens with")
        kid = header.get("kid")
        if kid is not None and not isinstance(kid, str):
            raise errorf(Code.INVALID_REQUEST, "token kid is not a string")
        pub = self._key(ctx, kid, alg)
        try:
            jwt.verify_jws(pub, alg, (parts[0] + "." + parts[1]).encode("ascii"), sig)
        except jwt.CryptoMissing as e:
            raise wrap_error(Code.UNSUPPORTED, e, 'verifying a token needs the cryptography package: pip install "toolpass[crypto]"') from e
        except ValueError:
            raise errorf(Code.INVALID_REQUEST, "token signature does not verify against the provider's keys") from None
        self._check_claims(claims, u.email)
        return claims

    def _check_claims(self, claims: dict[str, Any], email: str) -> None:
        if not _same_issuer(claims.get("iss"), self.issuer):
            raise errorf(Code.INVALID_REQUEST, f"token issuer {go_quote(str(claims.get('iss')))} is not {go_quote(self.issuer)}")
        aud = claims.get("aud")
        auds = aud if isinstance(aud, list) else [aud]
        if self.audience not in auds:
            raise errorf(Code.INVALID_REQUEST, f"token audience is not {go_quote(self.audience)}")
        now = self.now()
        exp = claims.get("exp")
        if not isinstance(exp, (int, float)) or isinstance(exp, bool):
            raise errorf(Code.INVALID_REQUEST, "token has no expiry")
        if exp + CLOCK_SKEW < now:
            raise errorf(Code.INVALID_REQUEST, "token has expired")
        nbf = claims.get("nbf")
        if isinstance(nbf, (int, float)) and not isinstance(nbf, bool) and nbf - CLOCK_SKEW > now:
            raise errorf(Code.INVALID_REQUEST, "token is not valid yet")
        claimed = claims.get(self.email_claim)
        if not isinstance(claimed, str) or not _same_email(claimed, email):
            raise errorf(Code.INVALID_REQUEST, f"the token's {self.email_claim} claim is not the check's user {email}")

    def _key(self, ctx: Context, kid: str | None, alg: str) -> Any:
        """The provider's public key for kid (or its only key of the
        algorithm's type), fetching the keys again once for a kid that is
        not in the cached set."""
        uri = self._jwks_uri(ctx)
        keys = self.keys.do(ctx, uri, lambda c: (self._fetch_keys(c, uri), KEYS_TTL))
        found = _select(keys, kid, alg)
        if found is None and self.now() - self.keys_fetched >= KEYS_RETRY:
            keys = self._fetch_keys(ctx, uri)
            self.keys.set(uri, keys, KEYS_TTL)
            found = _select(keys, kid, alg)
        if found is None:
            what = f"key {go_quote(kid)}" if kid is not None else f"a single {alg[:2]} key"
            raise errorf(Code.INVALID_REQUEST, f"the provider publishes no signing {what} for the token")
        try:
            return jwt.jwk_public_key(found)
        except ValueError as e:
            raise wrap_error(Code.UPSTREAM_ERROR, e, "the provider's signing key could not be read") from e

    def _jwks_uri(self, ctx: Context) -> str:
        doc = self.discovery.do(ctx, "", lambda c: (self._fetch_discovery(c), KEYS_TTL))
        return jsonx.s(doc, "jwks_uri")

    def _fetch_discovery(self, ctx: Context) -> dict[str, Any]:
        try:
            _, v = self.http.get_json(ctx, DISCOVERY_PATH)
            doc = jsonx.obj(v)
            if not _same_issuer(doc.get("issuer"), self.issuer):
                raise ValueError(f"the discovery document names issuer {go_quote(str(doc.get('issuer')))}, not {go_quote(self.issuer)}")
            uri = jsonx.s(doc, "jwks_uri")
            validate_https_url(uri)
        except ToolpassError:
            raise
        except ValueError as e:
            raise wrap_error(Code.UPSTREAM_ERROR, e, "the provider's discovery document is unusable") from e
        except Exception as e:
            raise _classify(e, "read the discovery document") from e
        return doc

    def _fetch_keys(self, ctx: Context, uri: str) -> dict[str, Any]:
        """The JWKS at uri as {kid: jwk}; keys without a kid under "".
        Only asymmetric signing keys count (kty RSA or EC, use absent or
        sig)."""
        try:
            _, v = self.http.get_json(ctx, uri)
            keys = jsonx.arr(jsonx.obj(v), "keys")
        except ToolpassError:
            raise
        except ValueError as e:
            raise wrap_error(Code.UPSTREAM_ERROR, e, "the provider's key set is unusable") from e
        except Exception as e:
            raise _classify(e, "read the signing keys") from e
        out: dict[str, Any] = {}
        for k in keys:
            if not isinstance(k, dict) or k.get("use", "sig") != "sig" or k.get("kty") not in ("RSA", "EC"):
                continue
            kid = k.get("kid")
            out[kid if isinstance(kid, str) else ""] = k
        self.keys_fetched = self.now()
        return out

    def probe(self, ctx: Context) -> ProbeResult:
        """Read the discovery document and the signing keys."""
        try:
            uri = self._jwks_uri(ctx)
            keys = self._fetch_keys(ctx, uri)
        except ToolpassError:
            raise
        except Exception as e:
            raise _classify(e, "read the provider's metadata") from e
        warnings: list[str] = []
        if not keys:
            warnings.append("the provider publishes no signing keys; every token will be refused")
        warnings.append("toolpass never calls the provider's user API: a check needs the user's ID token in its token field")
        return ProbeResult(summary=f"issuer {self.issuer} publishes {len(keys)} signing key(s) at {uri}", warnings=tuple(warnings))


def _select(keys: dict[str, Any], kid: str | None, alg: str) -> Any:
    if kid is not None:
        return keys.get(kid)
    kty = "EC" if alg.startswith("ES") else "RSA"
    same = [k for k in keys.values() if k.get("kty") == kty]
    return same[0] if len(same) == 1 else None


def _same_issuer(a: Any, b: str) -> bool:
    """Equal issuers, a trailing slash aside: a provider's iss may end in
    one (Auth0) while its URL is written without."""
    return isinstance(a, str) and a.rstrip("/") == b.rstrip("/")


def _same_email(a: str, b: str) -> bool:
    """Equal as the integrations match emails: ASCII case folded, nothing else."""
    a, b = go_trim_space(a), go_trim_space(b)
    if a.isascii() and b.isascii():
        return a.lower() == b.lower()
    return a == b


def _classify(err: BaseException, what: str) -> ToolpassError:
    he = httpx.classify(err)
    if he is not None:
        return he
    return wrap_error(Code.UPSTREAM_ERROR, err, f"could not {what}")


INTEGRATION = OIDC()

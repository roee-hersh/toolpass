"""The oidc integration against a fake provider: discovery, JWKS, and tokens
signed with keys made here. It answers no actions, so the coverage gate
asks for no action tests."""

from __future__ import annotations

import base64
import json
import time
from collections.abc import Callable, Iterator
from typing import Any

import pytest

from tests import harness as itest
from tests.integrations.argocd.test_argocd import Clock
from toolpass.authx import jwt
from toolpass.core.context import background
from toolpass.core.decision import Code, ToolpassError
from toolpass.core.integration import Connection, GroupsSource, User
from toolpass.integrations import kubernetes
from toolpass.integrations.argocd import Integration as ArgoIntegration
from toolpass.integrations.oidc import KEYS_RETRY, KEYS_TTL, OIDC, OIDCConnection

cryptography = pytest.importorskip("cryptography")

from cryptography.hazmat.primitives import hashes  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec, rsa, utils  # noqa: E402

AUD = "argo-cd"


def b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def int_b64(n: int, size: int) -> str:
    return b64(n.to_bytes(size, "big"))


class Provider:
    """The fake identity provider: one RSA and one EC signing key."""

    def __init__(self, srv: itest.Server) -> None:
        self.srv = srv
        self.issuer = srv.url
        self.rsa = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.ec = ec.generate_private_key(ec.SECP256R1())
        self.jwks_reads = 0
        self.discovery: dict[str, Any] = {"issuer": self.issuer, "jwks_uri": self.issuer + "/keys"}
        self.jwks: dict[str, Any] = {"keys": [self.rsa_jwk("k1"), self.ec_jwk("e1")]}
        self.jwks_status = 200
        srv.handle("GET", "/.well-known/openid-configuration", self._discovery)
        srv.handle("GET", "/keys", self._keys)

    def rsa_jwk(self, kid: str, key: Any = None) -> dict[str, Any]:
        pub = (key or self.rsa).public_key().public_numbers()
        return {"kty": "RSA", "kid": kid, "use": "sig", "alg": "RS256", "n": int_b64(pub.n, 256), "e": int_b64(pub.e, 3)}

    def ec_jwk(self, kid: str) -> dict[str, Any]:
        pub = self.ec.public_key().public_numbers()
        return {"kty": "EC", "kid": kid, "use": "sig", "crv": "P-256", "x": int_b64(pub.x, 32), "y": int_b64(pub.y, 32)}

    def _discovery(self, w: itest.ResponseWriter, r: itest.Request) -> None:
        w.write(json.dumps(self.discovery))

    def _keys(self, w: itest.ResponseWriter, r: itest.Request) -> None:
        self.jwks_reads += 1
        if self.jwks_status != 200:
            w.write_header(self.jwks_status)
            return
        w.write(json.dumps(self.jwks))

    def token(self, claims: dict[str, Any] | None = None, alg: str = "RS256", kid: str | None = "k1", key: Any = None, **over: Any) -> str:
        c: dict[str, Any] = {
            "iss": self.issuer,
            "sub": "00u1",
            "aud": AUD,
            "email": "dana@example.com",
            "exp": int(time.time()) + 300,
            "iat": int(time.time()),
            "groups": ["sre", "developers"],
        }
        c.update(claims or {})
        c.update(over)
        header: dict[str, Any] = {"alg": alg, "typ": "JWT"}
        if kid is not None:
            header["kid"] = kid
        signing = b64(json.dumps(header).encode()) + "." + b64(json.dumps(c).encode())
        if alg.startswith("ES"):
            der = self.ec.sign(signing.encode(), ec.ECDSA(hashes.SHA256()))
            rr, ss = utils.decode_dss_signature(der)
            sig = rr.to_bytes(32, "big") + ss.to_bytes(32, "big")
        elif alg == "HS256":
            sig = b"\x00" * 32
        elif alg == "none":
            sig = b""
        else:
            sig = jwt.sign(key or self.rsa, alg, signing.encode())
        return signing + "." + b64(sig)


Setup = Callable[..., "tuple[Provider, OIDCConnection, Clock]"]


@pytest.fixture
def setup() -> Iterator[Setup]:
    servers: list[itest.Server] = []

    def make(values: dict[str, str] | None = None) -> tuple[Provider, OIDCConnection, Clock]:
        srv = itest.Server()
        servers.append(srv)
        p = Provider(srv)
        clock = Clock()
        deps, _ = itest.deps(srv, now=clock)
        v = {"issuer": p.issuer, "audience": AUD, "groups_claim": "groups", "email_claim": "email"}
        v.update(values or {})
        c = OIDC().new(background(), itest.settings("sso", "oidc", v), deps)
        assert isinstance(c, OIDCConnection)
        return p, c, clock

    yield make
    for s in servers:
        s.close()


def groups(c: OIDCConnection, token: str, email: str = "dana@example.com") -> tuple[str, ...]:
    return c.user_groups(background(), User(email=email, token=token))


def refused(c: OIDCConnection, token: str, email: str = "dana@example.com") -> ToolpassError:
    with pytest.raises(ToolpassError) as ei:
        groups(c, token, email)
    return ei.value


def test_groups_from_a_verified_token(setup: Setup) -> None:
    p, c, _ = setup()
    assert isinstance(c, GroupsSource) and c.groups_claim_limit is None
    assert groups(c, p.token()) == ("sre", "developers")
    assert groups(c, p.token(alg="PS256")) == ("sre", "developers")
    assert groups(c, p.token(alg="ES256", kid="e1")) == ("sre", "developers")
    # The email is matched as the integrations match it: ASCII case folded.
    assert groups(c, p.token(email="Dana@Example.com")) == ("sre", "developers")
    # Discovery and the keys are read once for all of them.
    assert p.jwks_reads == 1
    # Duplicates and empties dropped; an absent claim is no groups.
    assert groups(c, p.token(groups=["a", "", "a", "b"])) == ("a", "b")
    assert groups(c, p.token({"groups": None})) == ()
    ident = c.resolve_identity(background(), User(email="dana@example.com", token=p.token()))
    assert ident.id == "00u1" and ident.groups == ("sre", "developers")


def test_aud_as_a_list_and_other_claim_names(setup: Setup) -> None:
    p, c, _ = setup({"groups_claim": "roles", "email_claim": "preferred_username"})
    tok = p.token({"aud": ["other", AUD], "roles": ["ops"], "preferred_username": "dana@example.com", "email": "someone@else.com"})
    assert groups(c, tok) == ("ops",)


@pytest.mark.parametrize(
    ("over", "want"),
    [
        ({"iss": "https://evil.example.com"}, "issuer"),
        ({"aud": "other"}, "audience"),
        ({"aud": ["other", "another"]}, "audience"),
        ({"exp": int(time.time()) - 600}, "expired"),
        ({"nbf": int(time.time()) + 600}, "not valid yet"),
        ({"email": "bob@example.com"}, "is not the check's user"),
        ({"groups": "sre"}, "not a list of strings"),
        ({"groups": ["sre", 1]}, "not a list of strings"),
    ],
)
def test_claims_are_checked(setup: Setup, over: dict[str, Any], want: str) -> None:
    p, c, _ = setup()
    e = refused(c, p.token(over))
    code = Code.UNSUPPORTED if "list" in want else Code.INVALID_REQUEST
    assert e.code == code and want in e.text, e


def test_no_expiry_is_refused(setup: Setup) -> None:
    p, c, _ = setup()
    tok = p.token({"exp": None})
    e = refused(c, tok)
    assert e.code == Code.INVALID_REQUEST and "no expiry" in e.text, e


def test_clock_skew(setup: Setup) -> None:
    p, c, clock = setup()
    tok = p.token({"exp": int(clock.now) + 1})
    assert groups(c, tok)
    clock.add(30)  # within the skew
    assert groups(c, tok)
    clock.add(60)
    assert "expired" in refused(c, tok).text


def test_bad_tokens(setup: Setup) -> None:
    p, c, _ = setup()
    e = refused(c, "")
    assert e.code == Code.INVALID_REQUEST and "no token" in e.text, e
    for bad in ("abc", "a.b", "a.b.c.d", "!!.!!.!!"):
        e = refused(c, bad)
        assert e.code == Code.INVALID_REQUEST, (bad, e)
    for alg in ("none", "HS256"):
        e = refused(c, p.token(alg=alg))
        assert e.code == Code.INVALID_REQUEST and "algorithm" in e.text, (alg, e)
    # A good header and payload with another key's signature.
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    e = refused(c, p.token(key=other))
    assert e.code == Code.INVALID_REQUEST and "signature" in e.text, e
    # A tampered payload.
    good = p.token()
    h, _, s = good.split(".")
    forged = h + "." + b64(json.dumps({"iss": p.issuer, "aud": AUD, "email": "dana@example.com", "exp": 9999999999, "groups": ["admin"]}).encode()) + "." + s
    assert "signature" in refused(c, forged).text
    # An EC token under an RSA kid, and the reverse.
    assert "signature" in refused(c, p.token(alg="ES256", kid="k1")).text
    assert "signature" in refused(c, p.token(alg="RS256", kid="e1")).text
    assert not [x for x in p.srv.calls() if x.path not in ("/.well-known/openid-configuration", "/keys")]


def test_key_rollover_fetches_the_keys_again_once(setup: Setup) -> None:
    p, c, clock = setup()
    assert groups(c, p.token())
    assert p.jwks_reads == 1
    # A kid the cached set lacks: fetched again once KEYS_RETRY has passed
    # since the last fetch, and the new key is used.
    new = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    p.jwks["keys"].append(p.rsa_jwk("k2", new))
    assert "k2" in refused(c, p.token(kid="k2", key=new)).text and p.jwks_reads == 1
    clock.add(KEYS_RETRY + 1)
    assert groups(c, p.token(kid="k2", key=new)) and p.jwks_reads == 2
    # Another unknown kid within KEYS_RETRY does not fetch again.
    e = refused(c, p.token(kid="k3", key=new))
    assert e.code == Code.INVALID_REQUEST and 'no signing key "k3"' in e.text and p.jwks_reads == 2, e
    clock.add(KEYS_RETRY + 1)
    refused(c, p.token(kid="k3", key=new))
    assert p.jwks_reads == 3
    # The set expires and is read again for an ordinary token.
    clock.add(KEYS_TTL + 1)
    assert groups(c, p.token(exp=int(clock.now) + 300)) and p.jwks_reads == 4


def test_token_without_kid_needs_a_single_key_of_its_type(setup: Setup) -> None:
    p, c, _ = setup()
    assert groups(c, p.token(kid=None))  # one RSA key
    p.jwks["keys"].append(p.rsa_jwk("k2"))
    p, c, _ = setup()
    p.jwks["keys"].append(p.rsa_jwk("k2"))
    e = refused(c, p.token(kid=None))
    assert "a single RS key" in e.text, e


def test_provider_failures(setup: Setup) -> None:
    p, c, _ = setup()
    p.discovery["issuer"] = "https://other.example.com"
    e = refused(c, p.token())
    assert e.code == Code.UPSTREAM_ERROR and "discovery" in e.text, e
    p, c, _ = setup()
    p.discovery["jwks_uri"] = "http://keys.example.com/keys"
    assert refused(c, p.token()).code == Code.UPSTREAM_ERROR
    p, c, _ = setup()
    p.jwks_status = 500
    e = refused(c, p.token())
    assert e.code == Code.UPSTREAM_ERROR, e
    p, c, _ = setup()
    p.jwks = {"keys": [{"kty": "oct", "kid": "k1", "k": "AAAA"}]}
    e = refused(c, p.token())
    assert e.code == Code.INVALID_REQUEST and "no signing key" in e.text, e
    p, c, _ = setup()
    p.jwks = {"keys": [{"kty": "RSA", "kid": "k1", "n": "AQAB", "e": "AQAB"}]}
    e = refused(c, p.token())
    assert e.code == Code.UPSTREAM_ERROR and "could not be read" in e.text, e
    # Keys marked for encryption are not signing keys.
    p, c, _ = setup()
    p.jwks["keys"][0]["use"] = "enc"
    assert "no signing key" in refused(c, p.token()).text


def test_check_answers_no_actions(setup: Setup) -> None:
    from toolpass.core.catalog import Action, parse_resource
    from toolpass.core.integration import CheckRequest, Identity

    _, c, _ = setup()
    assert OIDC().actions() == []
    req = CheckRequest(user=User(email="dana@example.com"), identity=Identity(), action=Action("x", "x"), action_name="x", resource=parse_resource("thing:1"))
    with pytest.raises(ToolpassError) as ei:
        c.check(background(), req)
    assert ei.value.code == Code.INVALID_REQUEST and "groups_connection" in ei.value.text


def test_probe(setup: Setup) -> None:
    p, c, _ = setup()
    r = c.probe(background())
    assert "2 signing key(s)" in r.summary and p.issuer in r.summary, r
    p, c, _ = setup()
    p.jwks = {"keys": []}
    r = c.probe(background())
    assert any("no signing keys" in w for w in r.warnings), r
    p, c, _ = setup()
    p.jwks_status = 503
    with pytest.raises(ToolpassError) as ei:
        c.probe(background())
    assert ei.value.code in (Code.UPSTREAM_ERROR, Code.UPSTREAM_RATE_LIMIT), ei.value


def test_settings(setup: Setup) -> None:
    p, c, _ = setup({"issuer": "https://acme.okta.com/oauth2/default/"})
    assert c.issuer == "https://acme.okta.com/oauth2/default"
    for bad in ({"issuer": "http://acme.okta.com"}, {"groups_claim": "a b"}, {"email_claim": ""}):
        s = itest.settings("sso", "oidc", {"issuer": p.issuer, "audience": AUD, **bad})
        f = {x.name: x for x in OIDC().fields()}
        key = next(iter(bad))
        with pytest.raises(ValueError):
            assert f[key].validate is not None
            f[key].validate(s.get(key))


def test_argocd_groups_from_the_token(setup: Setup) -> None:
    """argocd with groups_connection naming an oidc connection: the groups
    of the token the check carries decide."""
    from tests.integrations.argocd.test_argocd import check as argo_check
    from tests.integrations.argocd.test_argocd import default_cluster

    p, sso, _ = setup()
    srv = itest.Server()
    try:
        default_cluster().install(srv)
        deps, _ = itest.deps(srv)
        ks = itest.settings("k8s", "kubernetes", {"url": srv.url}, {"credential": itest.literal("k8s")})
        kc = kubernetes.Integration().new(background(), ks, deps)
        deps.connection = lambda id_: {"k8s": kc, "sso": sso}[id_]
        s = itest.settings(
            "argo",
            "argocd",
            {"kubernetes_connection": "k8s", "namespace": "argocd", "rbac_configmap": "argocd-rbac-cm", "user_subject": "none", "groups_connection": "sso"},
        )
        argo: Connection = ArgoIntegration().new(background(), s, deps)
        # (dana@example.com has a g line of her own in the policy; use another.)
        email = "sam@example.com"
        sam = User(email=email, token=p.token(email=email, groups=["sre"]))
        itest.expect_code(argo_check(argo, sam, "app.delete", "applications:dev/web"), Code.ALLOWED)
        dev = User(email=email, token=p.token(email=email, groups=["developers"]))
        itest.expect_code(argo_check(argo, dev, "app.delete", "applications:dev/web"), Code.DENIED)
        itest.expect_code(argo_check(argo, dev, "app.sync", "applications:dev/web"), Code.ALLOWED)
        # No token: unknown, never a deny or an allow.
        itest.expect_code(argo_check(argo, User(email=email), "app.sync", "applications:dev/web"), Code.INVALID_REQUEST)
        # Caller-sent groups still win over the token.
        both = User(email=email, groups=("developers",), token=p.token(email=email, groups=["sre"]))
        itest.expect_code(argo_check(argo, both, "app.delete", "applications:dev/web"), Code.DENIED)
    finally:
        srv.close()

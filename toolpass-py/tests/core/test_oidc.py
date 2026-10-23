"""core.oidc against a fake provider: discovery, JWKS, and tokens signed with
keys made here. Nothing provider-specific: the issuer, audience and claim
names are settings, the formats are OIDC Core's and JOSE's."""

from __future__ import annotations

import base64
import json
import time
from collections.abc import Callable, Iterator
from typing import Any

import pytest

from tests import harness as itest
from toolpass.authx import jwt
from toolpass.core.context import background
from toolpass.core.decision import Code, ToolpassError
from toolpass.core.integration import Deps, Settings, User
from toolpass.core.oidc import KEYS_RETRY, KEYS_TTL, Verifier, oidc_fields

pytest.importorskip("cryptography")

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, rsa, utils

AUD = "argo-cd"


def b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def int_b64(n: int, size: int) -> str:
    return b64(n.to_bytes(size, "big"))


class Clock:
    def __init__(self) -> None:
        self.now = time.time()

    def __call__(self) -> float:
        return self.now

    def add(self, d: float) -> None:
        self.now += d


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
            "sub": "u-1",
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


Setup = Callable[..., "tuple[Provider, Verifier, Clock, Deps]"]


def settings(values: dict[str, str]) -> Settings:
    return itest.settings("c", "argocd", values)


@pytest.fixture
def setup() -> Iterator[Setup]:
    servers: list[itest.Server] = []

    def make(values: dict[str, str] | None = None) -> tuple[Provider, Verifier, Clock, Deps]:
        srv = itest.Server()
        servers.append(srv)
        p = Provider(srv)
        clock = Clock()
        deps, _ = itest.deps(srv, now=clock)
        v = {"oidc_issuer": p.issuer, "oidc_audience": AUD, "oidc_groups_claim": "groups", "oidc_email_claim": "email"}
        v.update(values or {})
        v["oidc_issuer"] = v["oidc_issuer"].format(issuer=p.issuer)
        ver = Verifier.from_settings(settings(v), deps)
        assert ver is not None
        return p, ver, clock, deps

    yield make
    for s in servers:
        s.close()


def groups(v: Verifier, token: str, email: str = "dana@example.com") -> tuple[str, ...]:
    return v.token_groups(background(), User(email=email, token=token))


def refused(v: Verifier, token: str, email: str = "dana@example.com") -> ToolpassError:
    with pytest.raises(ToolpassError) as ei:
        groups(v, token, email)
    return ei.value


def test_groups_from_a_verified_token(setup: Setup) -> None:
    p, v, _, _ = setup()
    assert groups(v, p.token()) == ("sre", "developers")
    assert groups(v, p.token(alg="PS256")) == ("sre", "developers")
    assert groups(v, p.token(alg="ES256", kid="e1")) == ("sre", "developers")
    # The email is matched as the integrations match it: ASCII case folded.
    assert groups(v, p.token(email="Dana@Example.com")) == ("sre", "developers")
    # Discovery and the keys are read once for all of them.
    assert p.jwks_reads == 1
    # Duplicates and empties dropped; an absent claim is no groups.
    assert groups(v, p.token(groups=["a", "", "a", "b"])) == ("a", "b")
    assert groups(v, p.token({"groups": None})) == ()
    assert v.verify(background(), User(email="dana@example.com", token=p.token()))["sub"] == "u-1"


def test_issuer_trailing_slash(setup: Setup) -> None:
    # Some providers' iss ends in "/"; the URL is written either way.
    p, v, _, _ = setup({"oidc_issuer": "{issuer}/"})
    assert v.issuer == p.issuer
    p.discovery["issuer"] = p.issuer + "/"
    assert groups(v, p.token(iss=p.issuer + "/")) == ("sre", "developers")
    assert groups(v, p.token(iss=p.issuer)) == ("sre", "developers")


def test_distributed_groups_claim_is_unknown(setup: Setup) -> None:
    p, v, _, _ = setup()
    tok = p.token({"groups": None, "_claim_names": {"groups": "src1"}, "_claim_sources": {"src1": {"endpoint": "https://example.com/groups"}}})
    e = refused(v, tok)
    assert e.code == Code.UNSUPPORTED and "by reference" in e.text, e


def test_aud_as_a_list_and_other_claim_names(setup: Setup) -> None:
    p, v, _, _ = setup({"oidc_groups_claim": "roles", "oidc_email_claim": "preferred_username"})
    tok = p.token({"aud": ["other", AUD], "roles": ["ops"], "preferred_username": "dana@example.com", "email": "someone@else.com"})
    assert groups(v, tok) == ("ops",)


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
    p, v, _, _ = setup()
    e = refused(v, p.token(over))
    code = Code.UNSUPPORTED if "list" in want else Code.INVALID_REQUEST
    assert e.code == code and want in e.text, e


def test_no_expiry_is_refused(setup: Setup) -> None:
    p, v, _, _ = setup()
    e = refused(v, p.token({"exp": None}))
    assert e.code == Code.INVALID_REQUEST and "no expiry" in e.text, e


def test_clock_skew(setup: Setup) -> None:
    p, v, clock, _ = setup()
    tok = p.token({"exp": int(clock.now) + 1})
    assert groups(v, tok)
    clock.add(30)  # within the skew
    assert groups(v, tok)
    clock.add(60)
    assert "expired" in refused(v, tok).text


def test_bad_tokens(setup: Setup) -> None:
    p, v, _, _ = setup()
    e = refused(v, "")
    assert e.code == Code.INVALID_REQUEST and "no token" in e.text, e
    for bad in ("abc", "a.b", "a.b.c.d", "!!.!!.!!"):
        e = refused(v, bad)
        assert e.code == Code.INVALID_REQUEST, (bad, e)
    for alg in ("none", "HS256"):
        e = refused(v, p.token(alg=alg))
        assert e.code == Code.INVALID_REQUEST and "algorithm" in e.text, (alg, e)
    # A good header and payload with another key's signature.
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    e = refused(v, p.token(key=other))
    assert e.code == Code.INVALID_REQUEST and "signature" in e.text, e
    # A tampered payload.
    good = p.token()
    h, _, s = good.split(".")
    forged = h + "." + b64(json.dumps({"iss": p.issuer, "aud": AUD, "email": "dana@example.com", "exp": 9999999999, "groups": ["admin"]}).encode()) + "." + s
    assert "signature" in refused(v, forged).text
    # An EC token under an RSA kid, and the reverse.
    assert "signature" in refused(v, p.token(alg="ES256", kid="k1")).text
    assert "signature" in refused(v, p.token(alg="RS256", kid="e1")).text
    assert not [x for x in p.srv.calls() if x.path not in ("/.well-known/openid-configuration", "/keys")]


def test_key_rollover_fetches_the_keys_again_once(setup: Setup) -> None:
    p, v, clock, _ = setup()
    assert groups(v, p.token())
    assert p.jwks_reads == 1
    # A kid the cached set lacks: fetched again once KEYS_RETRY has passed
    # since the last fetch, and the new key is used.
    new = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    p.jwks["keys"].append(p.rsa_jwk("k2", new))
    assert "k2" in refused(v, p.token(kid="k2", key=new)).text and p.jwks_reads == 1
    clock.add(KEYS_RETRY + 1)
    assert groups(v, p.token(kid="k2", key=new)) and p.jwks_reads == 2
    # Another unknown kid within KEYS_RETRY does not fetch again.
    e = refused(v, p.token(kid="k3", key=new))
    assert e.code == Code.INVALID_REQUEST and 'no signing key "k3"' in e.text and p.jwks_reads == 2, e
    clock.add(KEYS_RETRY + 1)
    refused(v, p.token(kid="k3", key=new))
    assert p.jwks_reads == 3
    # The set expires and is read again for an ordinary token.
    clock.add(KEYS_TTL + 1)
    assert groups(v, p.token(exp=int(clock.now) + 300)) and p.jwks_reads == 4


def test_token_without_kid_needs_a_single_key_of_its_type(setup: Setup) -> None:
    p, v, _, _ = setup()
    assert groups(v, p.token(kid=None))  # one RSA key
    p, v, _, _ = setup()
    p.jwks["keys"].append(p.rsa_jwk("k2"))
    assert "a single RS key" in refused(v, p.token(kid=None)).text


def test_provider_failures(setup: Setup) -> None:
    p, v, _, _ = setup()
    p.discovery["issuer"] = "https://other.example.com"
    e = refused(v, p.token())
    assert e.code == Code.UPSTREAM_ERROR and "discovery" in e.text, e
    p, v, _, _ = setup()
    p.discovery["jwks_uri"] = "http://keys.example.com/keys"
    assert refused(v, p.token()).code == Code.UPSTREAM_ERROR
    p, v, _, _ = setup()
    p.jwks_status = 500
    assert refused(v, p.token()).code == Code.UPSTREAM_ERROR
    p, v, _, _ = setup()
    p.jwks = {"keys": [{"kty": "oct", "kid": "k1", "k": "AAAA"}]}
    assert "no signing key" in refused(v, p.token()).text
    p, v, _, _ = setup()
    p.jwks = {"keys": [{"kty": "RSA", "kid": "k1", "n": "AQAB", "e": "AQAB"}]}
    e = refused(v, p.token())
    assert e.code == Code.UPSTREAM_ERROR and "could not be read" in e.text, e
    p, v, _, _ = setup()
    p.jwks["keys"][0]["use"] = "enc"
    assert "no signing key" in refused(v, p.token()).text


def test_probe(setup: Setup) -> None:
    p, v, _, _ = setup()
    line, warnings = v.probe(background())
    assert "2 signing key(s)" in line and p.issuer in line and warnings == []
    p, v, _, _ = setup()
    p.jwks = {"keys": []}
    _, warnings = v.probe(background())
    assert any("no signing keys" in w for w in warnings)
    p, v, _, _ = setup()
    p.jwks_status = 503
    with pytest.raises(ToolpassError) as ei:
        v.probe(background())
    assert ei.value.code in (Code.UPSTREAM_ERROR, Code.UPSTREAM_RATE_LIMIT), ei.value


def test_from_settings() -> None:
    deps, _ = itest.deps()
    assert Verifier.from_settings(settings({}), deps) is None
    for bad, msg in (
        ({"oidc_issuer": "https://sso.example.com"}, "oidc_audience is required"),
        ({"oidc_audience": "x"}, "needs oidc_issuer"),
        ({"oidc_groups_claim": "roles"}, "needs oidc_issuer"),
    ):
        with pytest.raises(ValueError, match=msg):
            Verifier.from_settings(settings(bad), deps)
    v = Verifier.from_settings(settings({"oidc_issuer": "https://sso.example.com/", "oidc_audience": "x"}), deps)
    assert v is not None and v.issuer == "https://sso.example.com" and v.groups_claim == "groups" and v.email_claim == "email"
    fields = {f.name: f for f in oidc_fields("x")}
    for key, bad_value in (("oidc_issuer", "http://sso.example.com"), ("oidc_groups_claim", "a b")):
        f = fields[key]
        assert f.validate is not None
        with pytest.raises(ValueError):
            f.validate(bad_value)

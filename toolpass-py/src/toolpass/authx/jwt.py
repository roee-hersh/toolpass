"""JWT signing (RS256, PS256) and PEM key handling.

Signing needs the ``cryptography`` package (``pip install "toolpass[crypto]"``);
it is imported only when a key is parsed, so integrations that never sign
do not need it.
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
from dataclasses import dataclass, field
from typing import Any

from toolpass.authx.util import StructDict, go_json_loads, go_json_marshal, pem_decode
from toolpass.core.errors import go_quote

__all__ = [
    "ASYMMETRIC_ALGS",
    "PS256",
    "RS256",
    "CryptoMissing",
    "Header",
    "b64url_decode",
    "cert_thumbprint_sha256",
    "decode_jwt_claims",
    "jwk_public_key",
    "new_jti",
    "parse_certificate",
    "parse_rsa_private_key",
    "sign",
    "sign_jwt",
    "standard_claims",
    "verify",
    "verify_jws",
]

RS256 = "RS256"  # RSASSA-PKCS1-v1_5 with SHA-256 (GitHub Apps, Google, Salesforce)
PS256 = "PS256"  # RSASSA-PSS with SHA-256 (Microsoft Entra certificate credentials)


class CryptoMissing(RuntimeError):
    def __init__(self) -> None:
        super().__init__('signing and verifying with a key needs the cryptography package: pip install "toolpass[crypto]"')


def _crypto() -> Any:
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding, rsa
    except ImportError:
        raise CryptoMissing() from None
    return hashes, serialization, padding, rsa, x509


def _asymmetric() -> Any:
    """The elliptic-curve and signature helpers, after _crypto."""
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric import ec, utils
    except ImportError:
        raise CryptoMissing() from None
    return ec, utils, InvalidSignature


def _der_elements(der: bytes, n: int) -> list[int]:
    """The tags of the first n elements inside the outer DER SEQUENCE, or
    fewer when the encoding ends or is malformed."""
    tags: list[int] = []
    try:
        if der[0] != 0x30:
            return tags
        i = _der_header(der, 0)[0]
        while len(tags) < n and i < len(der):
            tags.append(der[i])
            start, length = _der_header(der, i)
            i = start + length
    except IndexError:
        pass
    return tags


def _der_header(der: bytes, i: int) -> tuple[int, int]:
    """(content start, content length) of the TLV at i."""
    ln = der[i + 1]
    if ln < 0x80:
        return i + 2, ln
    k = ln & 0x7F
    return i + 2 + k, int.from_bytes(der[i + 2 : i + 2 + k], "big")


def _der_kind(der: bytes) -> str:
    """pkcs1 (RSAPrivateKey), pkcs8 (PrivateKeyInfo), sec1 (ECPrivateKey)
    or "" by the shape of the structure."""
    tags = _der_elements(der, 2)
    if len(tags) < 2 or tags[0] != 0x02:
        return ""
    return {0x02: "pkcs1", 0x30: "pkcs8", 0x04: "sec1"}.get(tags[1], "")


_OTHER_FORMAT = {"pkcs1": "ParsePKCS1PrivateKey", "pkcs8": "ParsePKCS8PrivateKey", "sec1": "ParseECPrivateKey"}


def _load_der(der: bytes, want: str) -> Any:
    """x509.ParsePKCS1PrivateKey (want pkcs1) or ParsePKCS8PrivateKey (want
    pkcs8): the DER must be that structure."""
    _, serialization, _, _, _ = _crypto()
    kind = _der_kind(der)
    if kind != want:
        if kind in _OTHER_FORMAT:
            raise ValueError(f"x509: failed to parse private key (use {_OTHER_FORMAT[kind]} instead for this key format)")
        raise ValueError("x509: failed to parse private key: malformed DER")
    try:
        return serialization.load_der_private_key(der, password=None)
    except Exception as e:  # noqa: BLE001 - ValueError, TypeError or UnsupportedAlgorithm
        raise ValueError(f"x509: failed to parse private key: {e}") from None


def _go_key_type(k: Any) -> str | None:
    """The Go type ParsePKCS8PrivateKey returns for a non-RSA key, or None
    for an algorithm it does not know."""
    from cryptography.hazmat.primitives.asymmetric import ec, ed25519, x25519

    if isinstance(k, ec.EllipticCurvePrivateKey):
        return "*ecdsa.PrivateKey"
    if isinstance(k, ed25519.Ed25519PrivateKey):
        return "ed25519.PrivateKey"
    if isinstance(k, x25519.X25519PrivateKey):
        return "*ecdh.PrivateKey"
    return None


def parse_rsa_private_key(pem: bytes) -> Any:
    """A PEM private key: PKCS#1 ("RSA PRIVATE KEY") or PKCS#8 ("PRIVATE
    KEY"). Encrypted keys are not supported. The first block of one of
    these types decides; other blocks are skipped."""
    _, _, _, rsa, _ = _crypto()
    while True:
        block, rest = pem_decode(pem)
        if block is None:
            raise ValueError("no PEM private key found")
        if block.type == "RSA PRIVATE KEY":
            k = _load_der(block.bytes, "pkcs1")
            if not isinstance(k, rsa.RSAPrivateKey):
                raise ValueError("x509: failed to parse private key: not an RSA key")
            return k
        if block.type == "PRIVATE KEY":
            k = _load_der(block.bytes, "pkcs8")
            if not isinstance(k, rsa.RSAPrivateKey):
                t = _go_key_type(k)
                if t is None:
                    raise ValueError("x509: PKCS#8 wrapping contained private key with unknown algorithm")
                raise ValueError(f"PKCS#8 key is {t}, not RSA")
            return k
        if block.type == "ENCRYPTED PRIVATE KEY":
            raise ValueError("encrypted private keys are not supported; store the key unencrypted in a file secret")
        pem = rest


def parse_certificate(pem: bytes) -> Any:
    """The first PEM certificate."""
    _, _, _, _, x509 = _crypto()
    while True:
        block, rest = pem_decode(pem)
        if block is None:
            raise ValueError("no PEM certificate found")
        if block.type == "CERTIFICATE":
            try:
                return x509.load_der_x509_certificate(block.bytes)
            except Exception as e:  # noqa: BLE001
                raise ValueError(f"x509: malformed certificate: {e}") from None
        pem = rest


def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    """base64.RawURLEncoding.DecodeString: no padding, the URL alphabet
    only, CR and LF skipped."""
    s = s.replace("\r", "").replace("\n", "")
    m = re.search(r"[^A-Za-z0-9_-]", s)
    if m:
        raise ValueError(f"illegal base64 data at input byte {m.start()}")
    if len(s) % 4 == 1:
        raise ValueError(f"illegal base64 data at input byte {len(s) - 1}")
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def cert_thumbprint_sha256(cert: Any) -> str:
    """base64url SHA-256 of the DER certificate: the JOSE "x5t#S256" value."""
    _, serialization, _, _, _ = _crypto()
    return _b64url(hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).digest())


@dataclass
class Header:
    """The JOSE header. Extra keys go in extra."""

    alg: str
    typ: str = ""  # "JWT" unless set
    kid: str = ""
    x5t_s256: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


def sign_jwt(key: Any, h: Header, claims: Any) -> str:
    """Build and sign a compact JWS: header.payload.signature. claims is
    marshalled as JSON the way json.Marshal would (a plain dict is a Go map,
    keys sorted; a StructDict such as standard_claims returns keeps its
    order); bytes or str are the payload as they are."""
    if key is None:
        raise ValueError("no signing key")
    hdr: dict[str, Any] = {"alg": h.alg, "typ": h.typ or "JWT"}
    if h.kid:
        hdr["kid"] = h.kid
    if h.x5t_s256:
        hdr["x5t#S256"] = h.x5t_s256
    hdr.update(h.extra)
    if isinstance(claims, bytes):
        pb = claims
    elif isinstance(claims, str):
        pb = claims.encode("utf-8")
    else:
        pb = go_json_marshal(claims)
    signing_input = _b64url(go_json_marshal(hdr)) + "." + _b64url(pb)
    return signing_input + "." + _b64url(sign(key, h.alg, signing_input.encode("ascii")))


def sign(key: Any, alg: str, data: bytes) -> bytes:
    hashes, _, padding, _, _ = _crypto()
    if alg == RS256:
        return bytes(key.sign(data, padding.PKCS1v15(), hashes.SHA256()))
    if alg == PS256:
        return bytes(key.sign(data, padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32), hashes.SHA256()))
    raise ValueError(f"unsupported algorithm {go_quote(alg)}")


def verify(pub: Any, alg: str, data: bytes, sig: bytes) -> None:
    """Check a signature made by sign; raise on failure."""
    if alg not in (RS256, PS256):
        raise ValueError(f"unsupported algorithm {go_quote(alg)}")
    verify_jws(pub, alg, data, sig)


_JWS_HASHES = {"256": "SHA256", "384": "SHA384", "512": "SHA512"}
# The signature algorithms an identity provider signs ID tokens with and
# verify_jws accepts: RSA (PKCS#1 v1.5 and PSS) and ECDSA. "none" and the
# HMAC family are never accepted: they need no key, or the provider's secret.
ASYMMETRIC_ALGS = frozenset({"RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512"})


def verify_jws(pub: Any, alg: str, data: bytes, sig: bytes) -> None:
    """Check the signature of a compact JWS made with alg by the holder of
    pub (an RSA or EC public key); raise ValueError on failure or for an
    algorithm outside ASYMMETRIC_ALGS."""
    hashes, _, padding, rsa, _ = _crypto()
    ec, utils, invalid_signature = _asymmetric()
    if alg not in ASYMMETRIC_ALGS:
        raise ValueError(f"unsupported algorithm {go_quote(alg)}")
    h = getattr(hashes, _JWS_HASHES[alg[2:]])()
    try:
        if alg.startswith("ES"):
            if not isinstance(pub, ec.EllipticCurvePublicKey):
                raise ValueError(f"{alg} needs an EC key")
            n = (pub.curve.key_size + 7) // 8
            if len(sig) != 2 * n:
                raise ValueError("ecdsa: signature has the wrong length")
            der = utils.encode_dss_signature(int.from_bytes(sig[:n], "big"), int.from_bytes(sig[n:], "big"))
            pub.verify(der, data, ec.ECDSA(h))
            return
        if not isinstance(pub, rsa.RSAPublicKey):
            raise ValueError(f"{alg} needs an RSA key")
        pad: Any = padding.PKCS1v15() if alg.startswith("RS") else padding.PSS(mgf=padding.MGF1(h), salt_length=h.digest_size)
        pub.verify(sig, data, pad, h)
    except invalid_signature:
        raise ValueError("crypto/ecdsa: verification error" if alg.startswith("ES") else "crypto/rsa: verification error") from None


_JWK_CURVES = {"P-256": "SECP256R1", "P-384": "SECP384R1", "P-521": "SECP521R1"}


def jwk_public_key(jwk: Any) -> Any:
    """The public key of a JWK (RFC 7517): an RSA key from n and e, an EC
    key from crv, x and y. Anything else (a symmetric key, an unknown
    curve, a malformed member) is a ValueError."""
    _, _, _, rsa, _ = _crypto()
    ec, _, _ = _asymmetric()

    if not isinstance(jwk, dict):
        raise ValueError("jwk is not an object")
    kty = jwk.get("kty")

    def num(name: str) -> int:
        v = jwk.get(name)
        if not isinstance(v, str) or v == "":
            raise ValueError(f"jwk {kty} key lacks {name}")
        return int.from_bytes(_b64url_decode(v), "big")

    if kty == "RSA":
        n, e = num("n"), num("e")
        if n < (1 << 2047) or e < 3:
            raise ValueError("jwk RSA key is too small")
        return rsa.RSAPublicNumbers(e, n).public_key()
    if kty == "EC":
        curve = _JWK_CURVES.get(str(jwk.get("crv")))
        if curve is None:
            raise ValueError(f"jwk EC key has unsupported curve {go_quote(str(jwk.get('crv')))}")
        return ec.EllipticCurvePublicNumbers(num("x"), num("y"), getattr(ec, curve)()).public_key()
    raise ValueError(f"jwk key type {go_quote(str(kty))} is not an asymmetric signing key")


def standard_claims(
    iss: str = "",
    sub: str = "",
    aud: str = "",
    exp: int = 0,
    nbf: int = 0,
    iat: int = 0,
    jti: str = "",
    scope: str = "",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The registered claims most exchanges need (Go's StandardClaims),
    empty ones omitted. Without extra it marshals in field order, as the
    struct does; extra is merged in and the result is a plain dict that
    marshals with sorted keys, as the merged map does."""
    out: dict[str, Any] = StructDict()
    for k, v in (("iss", iss), ("sub", sub), ("aud", aud), ("exp", exp), ("nbf", nbf), ("iat", iat), ("jti", jti), ("scope", scope)):
        if v:
            out[k] = v
    if extra:
        merged = dict(out)
        merged.update(extra)
        return merged
    return out


def b64url_decode(s: str) -> bytes:
    """_b64url_decode for other packages: base64url without padding."""
    return _b64url_decode(s)


def decode_jwt_claims(token: str) -> Any:
    """The payload of a compact JWS, unverified. For reading the expiry out
    of tokens toolpass itself received."""
    parts = token.split(".", 2)
    if len(parts) != 3:
        raise ValueError("not a compact JWS")
    return go_json_loads(_b64url_decode(parts[1]))


def new_jti() -> str:
    """A random 128-bit identifier as base64url."""
    return _b64url(os.urandom(16))

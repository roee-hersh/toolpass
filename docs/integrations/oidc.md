# oidc

One connection is one OpenID Connect provider: Okta, Entra ID, Google, Keycloak, Auth0, Dex,
anything that publishes a discovery document. It answers no actions of its own. It serves as the
[`groups_connection`](../guides/groups.md) of the integrations that grant by group (argocd,
kubernetes, aws `static_map`) and reads the user's groups from the user's own ID token, the one
the system logged them in with, which the check carries in its `token` field. toolpass verifies
the token the way the system did and never calls the provider's user API, so it needs no
credential and sees exactly the groups the system saw.

## Credential

None. toolpass reads the provider's public discovery document and signing keys.

## Connection

```yaml
  - id: sso
    integration: oidc
    issuer: https://acme.okta.com/oauth2/default   # as in the system's OIDC settings
    audience: argo-cd                              # the client id the system logs in with
    groups_claim: groups                           # default
    email_claim: email                             # default

  - id: argocd-prod
    integration: argocd
    kubernetes_connection: k8s-prod-eu
    groups_connection: sso
```

| Key | Meaning |
|---|---|
| `issuer` | the provider's issuer URL, as the token's `iss` claim and the discovery document name it (`https://`; with or without the trailing `/` some providers, such as Auth0, put there) |
| `audience` | the client id the system logs in with; the token's `aud` must carry it, so a token issued for another application is refused |
| `groups_claim` | the claim holding the user's groups, as the system's own `oidc.config` names it |
| `email_claim` | the claim holding the user's email; it must equal the check's `user`, so a token cannot be paired with someone else's email |

## The token

The check sends the user's ID token in `token` ([HTTP API](../reference/api.md), the `token=`
argument of the [Python client](../reference/client.md), the `token` option of the Node client,
`Session(user, token=...)` and the `X-Toolpass-Token` header for the [toolkit](../reference/toolkit.md);
`toolpass.mcp.token_session` passes the token the MCP caller signed in with). It is the token the
application received when the user logged in through the provider, for the same client id the
system uses. toolpass never logs it and keys its caches by a digest of it.

## How the verification works

1. `GET <issuer>/.well-known/openid-configuration`; its `issuer` must equal `issuer` and its
   `jwks_uri` must be `https://`. Reused for an hour.
2. `GET <jwks_uri>`: the signing keys (`use` absent or `sig`) by `kid`. Reused for an hour; a token
   with a `kid` the set lacks fetches them again, at most once a minute (a key rollover).
3. The token must be a compact JWS signed with an RSA or ECDSA algorithm (`RS256`, `RS384`,
   `RS512`, `PS256`, `PS384`, `PS512`, `ES256`, `ES384`, `ES512`); `none` and the HMAC family are
   refused. The key is the one named by `kid`, or the provider's only key of the algorithm's type.
4. Claims: `iss` equals `issuer` (a trailing `/` aside); `aud` (a string or a list) carries `audience`; `exp` is present
   and not past, `nbf` not future, each with a minute of tolerance; `email_claim` equals the check's
   user (ASCII case folded).
5. The groups are `groups_claim`, a list of strings, each once; an absent claim is no groups.

A token that fails any of these is `invalid_request`: nothing about the user is known from it, so
the answer is unknown, never a deny or an allow.

## Decisions

| Situation | Answer |
|---|---|
| The token verifies | the groups go to the connection that asked |
| No token in the check, a token that does not parse, is signed with the wrong key or algorithm, has the wrong issuer or audience, has expired or is for another email | unknown (`invalid_request`) |
| `groups_claim` is not a list of strings, or is carried by reference (an Entra ID overage) | unknown (`unsupported`) |
| The discovery document names another issuer, or the keys cannot be read | unknown (`upstream_error`) |
| The provider cannot be reached | unknown (`upstream_timeout`, `upstream_error`, `upstream_rate_limited`) |

## What it cannot see

- Whether the token has been revoked, or the user signed out or been deactivated since it was
  issued: a token is good until `exp`, as it is to the system.
- A provider that puts the groups behind a reference rather than in the token (Entra ID's overage
  claim, above 200 groups) is answered unknown (`unsupported`): the system fetched them itself.
- The groups of a provider that only hands them out through its API (a `groups` scope the system
  did not request): then the token carries none.

## Unverified

Marked `# UNVERIFIED:` nowhere: the token formats and discovery are the OIDC and JOSE standards,
tested against keys made in the tests. Not yet confirmed against a live provider's tokens.

## Test

`toolpass-py/tests/integrations/oidc/test_oidc.py` runs against a fake provider over TLS with RSA
and EC keys made in the test: discovery, key rollover, every refusal, and argocd taking its groups
from the token.

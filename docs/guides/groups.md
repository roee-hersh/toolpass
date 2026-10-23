# Groups from the user's token

Some systems grant access by group: Kubernetes RBAC binds roles to the groups in the user's login,
Argo CD's policy matches `g, <group>, role:...` lines against them, and an aws `static_map` maps
groups to IAM roles. Those systems read the groups from the token the user logged in with, which the
identity provider fills from its groups claim. toolpass never sees that login, so for these systems
a check needs the user's groups as well as the email. There are two ways to supply them, and they
combine: groups the caller sends always win.

## The caller sends the groups

The `groups` field of a check ([HTTP API](../reference/api.md), [Python client](../reference/client.md))
carries the user's groups as the system sees them. This works with any identity provider: the
application that authenticated the user has their login and its groups claim, and passes the names
along.

## The caller sends the token: `oidc_issuer`

When the caller sends no groups, an integration that grants by group can read them from the user's
own ID token, the one the system logged them in with, sent in the check's `token` field. The
connection names the issuer and the client id the system uses, as they stand in the system's own
OIDC settings:

```yaml
connections:
  - id: argocd-prod
    integration: argocd
    kubernetes_connection: k8s-prod-eu
    oidc_issuer: https://sso.example.com      # Argo CD's oidc.config issuer
    oidc_audience: argo-cd                    # Argo CD's oidc.config clientID
    # oidc_groups_claim: groups               # default
    # oidc_email_claim: email                 # default
```

The same four keys go on every integration that grants by group: [argocd](../integrations/argocd.md),
[kubernetes](../integrations/kubernetes.md) and [aws](../integrations/aws.md) with
`identity_mode: static_map`. They work with every provider that publishes an OpenID Connect
discovery document, with no credential and no provider API, and see exactly the groups the system
saw, including whatever the provider filtered out of the claim. Nothing in toolpass knows one
provider from another.

### How the token is checked

Exactly as the system checked it when the user logged in:

1. `GET <issuer>/.well-known/openid-configuration`; its `issuer` must equal `oidc_issuer` and its
   `jwks_uri` must be `https://`. Reused for an hour.
2. `GET <jwks_uri>`: the signing keys (`use` absent or `sig`, RSA or EC) by `kid`. Reused for an
   hour; a token with a `kid` the set lacks fetches them again, at most once a minute (a key
   rollover).
3. The token must be a compact JWS signed with an RSA or ECDSA algorithm (`RS256`, `RS384`,
   `RS512`, `PS256`, `PS384`, `PS512`, `ES256`, `ES384`, `ES512`); `none` and the HMAC family are
   refused. The key is the one named by `kid`, or the provider's only key of the algorithm's type.
4. `iss` equals `oidc_issuer` (a trailing `/` aside); `aud` (a string or a list) carries
   `oidc_audience`; `exp` is present and not past, `nbf` not future, each with a minute of
   tolerance; `oidc_email_claim` equals the check's user (ASCII case folded), so a token cannot be
   paired with someone else's email.
5. The groups are `oidc_groups_claim`, a list of strings, each once; an absent claim is no groups.

A token that fails any of these, or no token at all, is `invalid_request`: nothing about the user
is known from it, so the answer is unknown, never a deny or an allow. A groups claim the provider
carries by reference (a distributed claim, `_claim_names`) is unknown (`unsupported`), since the
system fetched the groups itself.

### Sending the token

| Where | How |
|---|---|
| `POST /check` | the `token` field: printable ASCII without spaces, at most 16 KiB |
| Python client | `check(..., token=...)`, and `require`, `allowed`, `acheck`, `arequire` |
| Node client | `{ token }` in the options |
| The toolkit | `Session(user, token=...)`; `session_from_header` reads `X-Toolpass-Token`; `toolpass.mcp.token_session` passes the token the MCP caller signed in with |

The token is never logged; toolpass keys its caches on a digest of it.

### What it cannot see

- Whether the token has been revoked, or the user signed out or been deactivated since it was
  issued: a token is good until `exp`, as it is to the system.
- A groups claim the provider did not put in the token (a scope the system did not request, or a
  distributed claim): then the token carries none, or toolpass answers unknown.

## Kubernetes only: groups from the email

A kubernetes connection can also take a `groups_connection` that names the groups from the email
alone: an aws connection for EKS access entries, which are not in any token, or a directory. See
[kubernetes](../integrations/kubernetes.md#groups-from-the-email).

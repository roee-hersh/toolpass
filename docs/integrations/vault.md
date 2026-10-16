# vault

One connection is one HashiCorp Vault (one namespace). toolpass authenticates with a token or an
AppRole, finds the identity entity whose alias on the configured auth mount is the user's email,
collects the ACL policies attached to the entity, its groups (direct and inherited) and the auth
role (declared in the connection), reads each policy and evaluates the requested path and
capability with Vault's own rules. Nothing is written.

## Credential

Either of:

1. A **token** (`auth_mode: token`, the default), for instance a periodic service token.
2. An **AppRole** (`auth_mode: approle`): `role_id` and the `secret_id` as `credential`; toolpass
   logs in at `auth/<approle_mount>/login` and logs in again when the token expires or is revoked
   (at most once per token, so a permission denied on a path toolpass needs does not spend
   `secret_id` uses on every check).

Attach a policy with exactly these capabilities:

```hcl
path "identity/lookup/entity"     { capabilities = ["update"] }
path "identity/entity/id/*"       { capabilities = ["read"] }
path "identity/group/id/*"        { capabilities = ["read"] }
path "sys/policies/acl/*"         { capabilities = ["read"] }
path "sys/auth"                   { capabilities = ["read"] }
path "sys/mounts"                 { capabilities = ["read"] }
path "auth/token/lookup-self"     { capabilities = ["read"] }
```

`sys/auth` resolves the alias mount's accessor, `sys/mounts` the KV version of a mount. Nothing
under the secrets engines themselves is read.

`toolpass probe` looks the token up and resolves the alias mount's accessor.

## Connection

```yaml
  - id: vault-prod
    integration: vault
    url: https://vault.example.com:8200
    alias_mount: oidc/
    credential: env:VAULT_TOKEN
    # auth_mode: approle
    # role_id: 7f1c...
    # approle_mount: approle
    # token_policies: developers, oncall   # policies the oidc role attaches at login
    # namespace: admin/                    # Vault Enterprise, sent as X-Vault-Namespace
```

| Key | Meaning |
|---|---|
| `url` | the Vault address |
| `alias_mount` | the auth mount whose aliases carry the users' emails (`oidc/`, `ldap/`, `okta/`) |
| `credential` | the token or the AppRole `secret_id`, `env:` or `file:` |
| `auth_mode`, `role_id`, `approle_mount` | AppRole login |
| `token_policies` | policies every login through the alias mount receives (see below) |
| `namespace` | the Enterprise namespace |

### Identity

`POST identity/lookup/entity` with `alias_name` = the email and `alias_mount_accessor` = the
alias mount's accessor; no entity is `user_not_found`. Then `GET identity/entity/id/<id>` for the
entity's policies, `disabled` flag, metadata, aliases and group ids, and `GET identity/group/id/<id>`
for each direct and inherited group's name and policies. A disabled entity is denied every action;
an entity read that does not say whether it is disabled is unknown (`unsupported`).
The identity's groups are the group ids; its attributes carry the entity name, metadata, aliases and
group names so that policy templates can be resolved. Groups sent by the caller are ignored.

### Which policies

Vault attaches policies to a token from three places. toolpass sees two of them and takes the third
from configuration:

| Source | Read by toolpass |
|---|---|
| the entity's `policies` | yes |
| the policies of the entity's groups, direct and inherited (external groups included) | yes |
| the auth method role's `token_policies` (the OIDC role, the LDAP group mapping) | **no**: declare them in `token_policies` |
| `default` | assumed attached (UNVERIFIED for auth methods that exclude it) |

A policy named `root` on an entity or group answers `unsupported`: Vault refuses `root` alongside
other policies and never issues it through auth methods, so its presence is a misconfiguration
toolpass does not turn into allow (`root` is rejected in `token_policies`). A policy a token names
but Vault does not have contributes nothing, as in Vault.

## Resources

| Resource | Meaning |
|---|---|
| `kv:<mount>/<key>` | a secret in a KV engine; the mount is the longest `sys/mounts` prefix (mounts may span several segments), its KV version decides the API path (`<mount>/data/<key>`, `metadata`, `destroy` on v2; the logical path on v1) |
| `path:<api path>` | any API path, checked as written (`sys/seal`, `pki/issue/web`, `secret/data/x`) |
| `path:<api path>?namespace=<ns>` | the API path of a request made in namespace `<ns>` (its `X-Vault-Namespace` header; empty for none), as the [route table](#requests-a-toolpass-tool-makes) writes it |

Paths are plain segments (letters, digits, `_ . - @ : ~ =`); wildcards and dot-only segments are
rejected. `LIST` questions are evaluated both with the trailing slash Vault adds and without it, as
Vault does; an explicit deny on either form wins.

With `?namespace=`, the path is resolved the way Vault Enterprise resolves a request: the namespace
and the path joined are one path from the root (`team` + `secret/data/x` is `team/secret/data/x`,
the same request as `secret/data/x` with the header `team`). Under the connection's `namespace` the
rest is checked against its policies; a path into a child namespace keeps the child's name in front
(`child/secret/data/x`), as Vault applies a parent's policies to it. A path outside the connection's
namespace is unknown (`unsupported`): its policies live elsewhere. A non-empty `?namespace=` on a
connection without `namespace` is unknown too, because Vault Community ignores the header and
Enterprise applies it; name the namespace in the path instead (`team/secret/data/x`).

## Actions

| Action | Capability | KV v2 path |
|---|---|---|
| `secret.read` | `read` | `<mount>/data/<key>` |
| `secret.write` | `create` and `update` (see below) | `<mount>/data/<key>` |
| `secret.delete` | `delete` | `<mount>/data/<key>` |
| `secret.list` | `list` (prefix) | `<mount>/metadata/<key>/` |
| `secret.metadata` | `read` | `<mount>/metadata/<key>` (v2 only) |
| `secret.destroy` | `update` | `<mount>/destroy/<key>` (v2 only) |
| `raw:<capability>` | `read`, `create`, `update`, `patch`, `delete`, `list`, `sudo`, `subscribe`, `recover` on a `path:` |

A **write** is `create` for a new secret and `update` for an existing one. When the policies grant
both the answer is `allowed`, neither `denied`, one of the two `unsupported` (the write succeeds
only if the secret does or does not exist yet).

### Evaluation

Vault's documented rules, applied to the union of the policies' stanzas:

1. Stanzas whose path matches the request path are candidates: an exact path, a segment that is
   exactly `+` for any one segment, a trailing `*` for any suffix. `*` elsewhere and `+` inside a
   segment are literal; one leading `/` is dropped, as Vault does.
2. The highest-priority pattern wins: the one whose first wildcard comes latest, then one without a
   trailing glob, then fewer `+`, then longer, then lexicographically greater. The same pattern in
   several policies takes the union of its capabilities.
3. `deny` in the winning stanza denies. Otherwise the needed capability must be present.
4. `{{identity.entity.id}}`, `.name`, `.metadata.<k>`, `.aliases.<accessor>.id|name|metadata.<k>`,
   `{{identity.groups.ids.<id>.name}}` and `{{identity.groups.names.<name>.id}}` are resolved for
   the user. From a template that cannot be resolved (another selector, an empty value, a value with
   a slash or wildcard) the stanza matches everything under its literal prefix, and if such a stanza
   matches the request at all the answer is `unsupported`.
5. `allowed_parameters`, `denied_parameters`, `required_parameters`, `min_wrapping_ttl` and
   `max_wrapping_ttl` on the winning stanza answer `unsupported`: toolpass does not see the request's
   parameters (reads carry them too, such as KV's `version`).

Policies in HCL (including the deprecated `policy = "read|write|sudo|deny"` attribute,
`path = { ... }` maps and nested `control_group` blocks, which are skipped) and in JSON (object and
list forms) are parsed. Heredocs and other syntax answer `unsupported`.

## Requests a toolpass tool makes

The [secure-tools toolkit](../guides/intercepted-actions.md) maps every request a tool's body sends
to `<url>/v1/...` to the capability Vault itself checks on the path, whatever engine is mounted
there, and checks it for the session's user (`toolpass routes vault` prints the table):

| Request | Action | Effect |
|---|---|---|
| `GET`, `HEAD` | `raw:read`; with `?list=true`, `raw:list` | read |
| `LIST` | `raw:list` | read |
| `POST`, `PUT` | `secret.write`: `create` and `update`, since which one Vault checks depends on whether the path exists; `raw:update` on an endpoint of `sys/`, `identity/` or `auth/token/` that has no existence check (see below) | write (destructive under a `delete`, `destroy` or `revoke…` segment) |
| `PATCH` | `raw:patch` | write |
| `DELETE` | `raw:delete` | destructive |

The resource is `path:<request path>?namespace=<the X-Vault-Namespace header>`.

Vault treats a `POST` or `PUT` as a create only on an endpoint with an existence check (marked
`x-vault-createSupported` in Vault's API description) whose target does not exist yet, and as an
update everywhere else. Which endpoint a path reaches is certain only in the mounts Vault itself
fixes, `sys/`, `identity/` and `auth/token/`, so there an endpoint the description lists without an
existence check needs `update` alone: `sys/wrapping/unwrap`, `sys/policies/acl/<name>`,
`sys/mounts/<path>`, `identity/entity`, `auth/token/revoke-accessor` and the like. The ones with an
existence check (`auth/token/roles/<role>`, `identity/oidc/key/<name>` and the other OIDC objects,
`sys/managed-keys/...`), endpoints the description does not list, and every path in a mount an
operator named need both, since such a mount may be KV v1, where every path has an existence check.
Name the namespace in the `X-Vault-Namespace` header rather than the path for this to apply inside
one: `team/sys/...` may be a mount named `team`. A policy that grants only `update` on a path that
needs both (a KV write, `transit/encrypt`, which creates a missing key, or `pki/issue`, whose mount
toolpass cannot tell from a KV mount) answers unknown: grant `create` too or make the call inside
`as_agent()`. `auth/token/lookup-self`, `sys/capabilities-self`, `sys/internal/ui/resultant-acl`
(the agent's own token) and `sys/health`, `sys/seal-status`, `sys/leader` need no user permission.

Refused, at any depth of the path, since a leading part may be a namespace (`team/cubbyhole/x`):

- **What Vault scopes to the calling token**, which is the agent's: `cubbyhole/`,
  `auth/token/renew-self` and `revoke-self`, the token's TOTP MFA secret, `identity/oidc/token/*`,
  an OIDC provider's `authorize`, `sys/control-group/authorize`.
- **Token creation** (`auth/token/create`, `create-orphan`, `create/<role>`): the new token inherits
  the agent's policies.
- **Paths that need `sudo`** besides the capability: Vault's root-protected paths (`sys/seal`,
  `sys/auth/*`, `sys/audit`, `sys/raw`, `sys/remount`, `sys/leases/revoke-prefix/*`, ...,
  `auth/token/accessors`, `revoke-orphan`) and a PKI root (`DELETE <mount>/root`,
  `<mount>/root/sign-self-issued`; a KV key named `root` is refused on delete with it).
- **Operator operations** Vault does not authorize by policy: init, unseal, rekey, root generation,
  writes to `sys/replication/*` and `sys/license/*`.
- **What toolpass cannot read exactly**: an empty or dot-only segment, characters outside the plain
  set, a trailing slash on anything but a list, a `?list=` that is not a boolean or is given twice,
  a query Go would read differently, a namespace header that is not a namespace path.

A tool that logs in (`auth/<mount>/login`) is checked like any other `POST`, so users' policies
rarely allow it: log in before the tool runs, or inside `as_agent()`.

## Decisions

| Code | When |
|---|---|
| `allowed` | the winning stanza grants the capability |
| `denied` | no stanza matches; the winning stanza denies or lacks the capability; the entity is disabled |
| `unsupported` | a parameter or wrapping constraint; an unresolvable template; a policy toolpass cannot parse; the `root` policy; a KV v2 question on a v1 mount; a non-KV mount asked with `kv:`; a mixed create/update write; a `?namespace=` path outside the connection's namespace, or with a namespace on a connection without one |
| `resource_not_visible` | `kv:` names no mount `sys/mounts` lists, or a mount without a key |
| `user_not_found` | no entity has the alias |
| `credential_rejected` | 403 permission denied on a read toolpass needs; the AppRole login fails |
| `invalid_request` | a malformed path or resource; `alias_mount` is not an enabled auth method |
| `upstream_*` | 5xx, 429, 412, timeouts, a sealed Vault |

## What it cannot see

- **Token policies from auth roles**, unless declared in `token_policies`.
- **Sentinel** (EGP and RGP) policies, **control groups**, **MFA** enforcement.
- **Request parameters**: `allowed_parameters` and friends.
- **Namespaces above the connection's**: policies granted in a parent namespace on child paths.
  Under a `namespace`, a policy of the entity or its groups that answers 404 may be such a policy,
  so the answer is unknown (`unsupported`) rather than a check without it; in the root namespace a
  policy Vault does not have is skipped, as Vault skips it.
- **Token-specific state**: TTLs, uses, bound CIDRs, `sudo` paths' `x-vault-sudo` requirement is
  not checked unless asked with `raw:sudo`.
- **Path aliases** resolved by engines (e.g. `secret/foo` on KV v2 rewritten by the CLI): toolpass
  checks the API path, so use `kv:` for KV engines.
- **Policies held inside a child namespace** for a request into it: the connection namespace's
  policies are evaluated on the child path. When the entity lists a group that lives in the child
  namespace, the group cannot be read and the check fails rather than skip it.
- **A repeated `X-Vault-Namespace` header**: Vault reads the first, the hook keeps the last value a
  client library hands it.

## Unverified

Written from Vault's documentation source (policies, identity, system API) and the OpenAPI document
in `vault-client-go`; not run against a live Vault. Marked `UNVERIFIED` in the code where it
matters:

- The `default` policy is attached to every token of the alias mount.
- The capabilities the deprecated `policy = "read|write|sudo"` attribute maps to.
- `identity/lookup/entity` answers 204 for no match (an empty `data` is also taken as none).
- `sys/policies/acl/<name>` wraps `policy` in `data` (the top-level form is also read).

## Test

`python -m pytest tests/integrations/vault` (in `toolpass-py`) runs a fake Vault validated against the OpenAPI document
when `TOOLPASS_SPECS_DIR` holds `vault.spec` (`test/specs/fetch.sh`). The fake has an OIDC mount,
entities with direct and inherited groups, a disabled entity, a `root` entity, HCL and JSON
policies with globs, `+` segments, templates, a legacy `policy` attribute, parameter and wrapping
constraints, KV v1 and v2 mounts and a PKI mount, and an AppRole login. Property tests
(`test_fuzz_parse_target`, `test_fuzz_policy`) check the path validation and that the policy parser
and evaluator never fail with anything but a policy error. `tests/integrations/vault/test_routes.py`
holds the route table to the OpenAPI document, its refusals, and its resources to the check itself
against the fake. `tests/real/test_vault_real.py` starts
`vault server -dev` in Docker (`hashicorp/vault:1.17`, when the image is present locally), sets up
policies, entities, aliases and groups through its API, and checks every answer against Vault's own
`sys/capabilities`.

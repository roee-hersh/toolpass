# okta

One connection is one Okta org. toolpass authenticates with an Okta API token, finds the user by
login (the email) and lists the groups the user belongs to. It answers one action, `group.member`,
and serves as the `groups_connection` of a [kubernetes](kubernetes.md#groups-from-the-email) or
[argocd](argocd.md#groups-from-the-email) connection whose OIDC groups claim comes from Okta.
Nothing is written.

## Credential

An **API token** (Security, API, Tokens) created while signed in as a **Read-only Administrator**:
an Okta token acts with the admin role of the admin who created it, and a read-only admin can read
users and groups. Reference it with `env:` or `file:`; it is sent as `Authorization: SSWS <token>`.

`toolpass probe` lists one user to prove the token works and always reminds that the token carries
its creator's admin role.

## Connection

```yaml
  - id: okta-main
    integration: okta
    url: https://acme.okta.com
    credential: env:OKTA_API_TOKEN
```

| Key | Meaning |
|---|---|
| `url` | the Okta org URL, `https://<org>.okta.com` (or a custom domain) |
| `credential` | the API token, `env:` or `file:` |

### Identity

`GET /api/v1/users/{email}`: Okta finds a user by id, login or login shortname, so the record is
accepted only when its `profile.login` equals the email, ignoring case. A 404, or a record with
another login, is `user_not_found`. Groups sent by the caller are ignored.

## Resources

| Resource | Meaning |
|---|---|
| `group:<name>` | an Okta group by its `profile.name`, e.g. `group:platform-team` |
| `group:<id>` | an Okta group by id, e.g. `group:00g1abcd2EFGH3ijk4x7` |

## Actions

| Action | Resource | Okta calls |
|---|---|---|
| `group.member` | `group:` | `GET /api/v1/users/{id}/groups?limit=200`, every page (Link `rel="next"`, kept on the org's host) |

## Decisions

| Okta says | toolpass answers |
|---|---|
| the group (by name or id) is in the user's list | allow |
| it is not | deny |
| the user is `STAGED`, `PROVISIONED`, `SUSPENDED` or `DEPROVISIONED` (cannot sign in) | deny |
| no user has the email as login | deny (`user_not_found`) |
| 401, 403 | unknown (`credential_rejected`) |
| 429, 5xx, a next link off the org | unknown (`upstream_rate_limited`, `upstream_error`) |

## Kubernetes and Argo CD groups

As a kubernetes or argocd `groups_connection`, the connection returns the names of the user's groups,
which is what an Okta OIDC app's groups claim carries. A user who cannot sign in (staged,
provisioned, suspended, deprovisioned) is `user_not_found`.
The Kubernetes username stays the kubernetes connection's `username_template`.

## Requests a tool makes

Reading a group (`GET /api/v1/groups/{id}`, its `users` and `apps`) is checked as `group.member` on
that group by id. Every write under `/api/v1` (users, groups, memberships, apps, policies) is
administration no action covers, and is refused. Other reads return what the agent's own token
sees, untrusted.

## What it cannot see

- An app's groups claim is usually filtered (a regex or a prefix in the claim's settings); toolpass
  returns every group, so a binding to a group the claim leaves out reads as allowed here while the
  cluster (or Argo CD) denies it.
- Group rules and app assignments that are pending are not in the user's list until Okta applies them.

## Unverified

Marked `# UNVERIFIED:` in the code:

- That the groups claim of an Okta OIDC app carries the groups' `profile.name`, as Okta documents
  for the default claim.

## Test

Unit tests run against a fake Okta org: lookup by login, the login-shortname guard, pagination over
the Link header (a next link to another host is refused), inactive users, a rejected token, and the
route table. No Okta API description is fetched by `test/specs/fetch.sh`, so these requests are not
validated against one.

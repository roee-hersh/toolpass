# Groups from the email

Some systems grant access by group: Kubernetes RBAC binds roles to the groups in the user's login,
Argo CD's policy matches `g, <group>, role:...` lines against them, and an aws `static_map` maps
groups to IAM roles. Those systems read the groups from the user's login token, which the identity
provider (Okta, Entra ID, Google, Keycloak, Dex, ...) fills from its groups claim. toolpass never
sees that token, so for these systems a check needs the user's groups as well as the email.

There are two ways to supply them.

## The caller sends the groups

The `groups` field of a check ([HTTP API](../reference/api.md), [Python client](../reference/client.md))
carries the user's groups as the system sees them. This works with any identity provider: the
application that authenticated the user has their login and its groups claim, and passes the names
along. Groups the caller sends are always used as they are; the connection does not look further.

## `groups_connection`: toolpass asks a directory

When the caller sends no groups, a connection that grants by group can look them up itself from a
directory, so the email alone is enough. Every such integration takes the same key:

```yaml
connections:
  - id: okta-main
    integration: okta
    url: https://acme.okta.com
    credential: env:OKTA_API_TOKEN

  - id: argocd-prod
    integration: argocd
    kubernetes_connection: k8s-prod-eu
    groups_connection: okta-main       # the user's Okta groups when the caller sends none

  - id: k8s-prod-eu
    integration: kubernetes
    url: https://10.20.0.5:6443
    credential: file:/secrets/k8s-prod-eu-token
    groups_connection: okta-main       # the same directory serves every integration
```

### Which integrations take it

| Integration | What the groups are matched against |
|---|---|
| [kubernetes](../integrations/kubernetes.md#groups-from-the-email) | the groups of the `SubjectAccessReview`, with `group_prefix` in front; also accepts an aws connection, which names EKS access entries instead |
| [argocd](../integrations/argocd.md#groups-from-the-email) | the group values of the RBAC policy |
| [aws](../integrations/aws.md#identity-modes) with `identity_mode: static_map` | the group rows of `role_map_file` |

### Which connections can be one

Any connection whose integration can name a user's groups from the email; `toolpass catalog
<integration>` lists the accepted ones for the key, and `toolpass validate` refuses any other.

| `groups_connection` names | What it returns | Matches the groups claim of |
|---|---|---|
| [okta](../integrations/okta.md) | the names of the user's Okta groups (unknown above 100) | an Okta OIDC app |
| [microsoft365](../integrations/microsoft365.md) | the object ids of the user's Entra ID groups, lower-cased, transitive (unknown above 200) | Entra ID |
| [googleworkspace](../integrations/googleworkspace.md) | the email addresses of the user's Google groups | Google (through Dex or another broker); for GKE, see [kubernetes](../integrations/kubernetes.md#groups-from-the-email) |
| [aws](../integrations/aws.md#eks-access-entries) | the `kubernetesGroups` of the user's EKS access entries; kubernetes connections only | EKS |
| `fake` | the `groups` field of the fake connection | tests |

### How it behaves

- Groups the caller sends win; the directory is not asked.
- The lookup is part of the identity, so it is cached with it (`identity_cache_seconds`, per
  connection, email and caller-sent groups), not on every check.
- A user the directory does not know, or one who cannot sign in (an Okta user who is staged,
  provisioned, suspended or deprovisioned; a disabled Entra ID account; a suspended or archived
  Google account), is `user_not_found`, a deny: such a user could not log in to the system either.
- A user in no group is checked as a user in no group, which is what the system would see.
- Argo CD and Kubernetes treat directory groups like groups a caller sent: in Kubernetes
  `group_prefix` is put in front and a `system:` group is refused without one.

### What it cannot see

A provider's groups claim carries only so many groups: Okta leaves it out of the token for a user in
more than 100 groups, and Entra ID replaces it with an "overage" claim above 200 (UNVERIFIED, both).
Such a user is answered unknown (`unsupported`) rather than evaluated against groups the system
never saw. Google's claim has no documented cap.

An app's groups claim is often filtered: an Okta claim's regex or prefix, an Entra ID app's "groups
assigned to the application", a Dex connector's group allow-list. toolpass returns every group the
user is in, so a binding to a group the claim leaves out reads as allowed here while the system
denies it. Write the bindings only with groups the claim carries.

A provider toolpass has no integration for (Keycloak, Auth0, LDAP, a Dex connector such as GitHub)
cannot be a `groups_connection`: OIDC has no standard way to ask a provider for another user's
groups. Have the caller send the groups for those users.

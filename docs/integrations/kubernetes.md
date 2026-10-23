# kubernetes

toolpass asks the API server with a `SubjectAccessReview`: "may user U with groups G do verb V on
resource R in namespace N?". The API server answers from RBAC and every other authorizer it has
(webhooks, ABAC, Node). Nothing is persisted.

## Credential

A ServiceAccount whose only permission is creating SubjectAccessReviews. That is one ClusterRole rule:

```yaml
rules:
  - apiGroups: ["authorization.k8s.io"]
    resources: ["subjectaccessreviews"]
    verbs: ["create"]
```

`test/kind/toolpass-rbac.yaml` has the full ServiceAccount, ClusterRole and ClusterRoleBinding. This
is tighter than the built-in `system:auth-delegator`, which also allows TokenReviews.

A projected token (`kubectl create token toolpass -n toolpass`, or a projected volume in a pod) expires;
reference it with `credential: file:/path` and toolpass re-reads it on every use.

## Connection

```yaml
  - id: k8s-prod-eu
    integration: kubernetes
    url: https://10.20.0.5:6443
    ca_file: /etc/toolpass/ca/prod-eu.pem     # the cluster CA
    tls_server_name: kubernetes                # when the URL is an IP
    credential: file:/secrets/k8s-prod-eu-token
    username_template: "oidc:{email}"          # optional, default {email}
    group_prefix: "oidc:"                      # optional
    add_authenticated_group: "true"            # optional, default true
    oidc_issuer: https://sso.example.com       # optional: read the groups from the user's ID token
    oidc_audience: kubernetes                  #   (the API server's --oidc-issuer-url and --oidc-client-id)
    # groups_connection: aws-prod              # or: EKS access entries / a directory, from the email
```

| Key | Meaning |
|---|---|
| `url` | API server URL |
| `credential` | ServiceAccount token, `env:` or `file:` |
| `username_template` | How the API server names your users. Placeholders `{email}`, `{local}`, `{domain}`. Must contain `{email}` or `{local}`. A name that comes out starting with `system:` (Kubernetes' own ServiceAccounts and components) is refused (`invalid_request`). `{local}` drops the domain, so it suits a single-domain directory |
| `group_prefix` | Prefix the API server puts on groups from your identity provider. Without it a caller group starting with `system:` is rejected (`invalid_request`), since it would name a built-in group such as `system:masters` |
| `add_authenticated_group` | Also send `system:authenticated`, as the API server would for any logged-in user |
| `oidc_issuer`, `oidc_audience`, `oidc_groups_claim`, `oidc_email_claim` | Read the user's groups from the ID token sent with the check when the caller sends none: the API server's `--oidc-issuer-url`, `--oidc-client-id` and `--oidc-groups-claim` (or the structured authentication config). See [Groups from the user's token](../guides/groups.md). Not with `groups_connection` |
| `groups_connection` | The id of an `aws`, `googleworkspace`, `microsoft365`, `okta` or `fake` connection that names the user's groups from the email when the caller sends none. See [Groups from the email](#groups-from-the-email) |
| `eks_cluster` | The EKS cluster name. Required when `groups_connection` is an `aws` connection, refused otherwise |
| `eks_aws_auth` | `true` also reads the `kube-system/aws-auth` ConfigMap for principals with no access entry (default `false`; needs one more RBAC rule, below) |

toolpass has no user directory to consult. The Kubernetes username is a pure transform of the email, so
the template must match the API server's `--oidc-username-prefix` / `--oidc-username-claim` flags or its
structured authentication config. A wrong template silently denies everyone. Run one check for a user
you know is allowed after configuring.

Groups come from the caller's `groups` field, or, when the caller sends none, from the user's ID
token (`oidc_issuer`, exactly what the API server's OIDC authenticator sees, `group_prefix` put in
front) or from `groups_connection` (next section). Kubernetes has no user directory: a
review sent with no groups is evaluated for a user in no group, so on a cluster that grants access
through groups every such check is a false deny. Caller-sent groups always win, so an existing caller
sees no change. Every holder of the API key can assert any group in the `groups` field. Set
`group_prefix` to match your identity provider; then a caller's value can never name a group the API
server reserves. Without a prefix, groups starting with `system:` are rejected with
`invalid_request` rather than sent.

## Groups from the email

With `groups_connection` set, toolpass asks that connection for the user's groups whenever the
caller sends none, so the email alone is enough:

| `groups_connection` names | What toolpass sends | Fits |
|---|---|---|
| [`aws`](aws.md) (with `eks_cluster`) | the `kubernetesGroups` of the EKS access entries of the user's IAM principals (the Identity Center roles of their permission sets, their IAM user, or their `role_map_file` roles), unioned; the entry's `username` when exactly one entry matched | EKS |
| [`googleworkspace`](googleworkspace.md) | the email addresses of the user's Google groups (`gke_security_group` on that connection keeps only the groups GKE honours), and the account's primary email as the username | GKE with Google Groups for RBAC |
| [`microsoft365`](microsoft365.md) | the object ids of the user's Entra groups, lower-cased, transitive | AKS with Entra ID integration |
| [`okta`](okta.md) | the names of the user's Okta groups | an API server whose OIDC groups claim comes from Okta |
| `fake` | the `groups` field of the fake connection | tests |

Directory groups (Google, Entra, Okta) are treated exactly like groups a caller sent: `group_prefix`
is put in front, and without a prefix a group starting with `system:` is refused. EKS access entries
are different: their `kubernetesGroups` are Kubernetes group names the cluster admin chose, so they
are sent as they are, with no prefix, and `system:masters` is allowed. A username a source names
replaces `username_template`; a username starting with `system:` is refused either way.

A user the source does not know, a suspended or disabled one, and (EKS) an IAM principal with no
access entry are answered `user_not_found`, a deny, naming the principals that were tried. A
directory user in no group is checked by username alone.

### EKS

```yaml
  - id: aws-prod
    integration: aws
    # ... as in aws.md; eks_region if the cluster is not in `region`
  - id: k8s-eks-prod
    integration: kubernetes
    url: https://ABCDEF0123456789.gr7.eu-west-1.eks.amazonaws.com
    ca_file: /etc/toolpass/ca/eks-prod.pem
    credential: file:/secrets/eks-prod-token
    groups_connection: aws-prod
    eks_cluster: prod
    eks_aws_auth: "true"      # optional: clusters still mapping users in aws-auth
```

The aws connection's account role needs `eks:DescribeAccessEntry` on the cluster (see
[aws](aws.md#eks-access-entries)). Each principal ARN is looked up as it is and then with its IAM
path removed, since `AWSReservedSSO_*` roles have a path and EKS (UNVERIFIED for access entries,
documented for aws-auth) matches them without it. In a username, `{{AccountID}}` is filled from the
principal and `{{SessionName}}` / `{{SessionNameRaw}}` with the email (UNVERIFIED: the session name
of an Identity Center role is taken to be the email, with `@` turned into `-` for `{{SessionName}}`);
any other placeholder leaves the username to `username_template`.

A user with several principals (several permission sets) gets the groups of all their entries
together. A real session is one role at a time, but RBAC only adds permissions, so one action is
allowed for the union exactly when one of the roles allows it; only bindings to the per-role
usernames are lost, since no single username applies then.

With `eks_aws_auth: "true"`, each principal with no access entry is also looked up in the `mapRoles`
and `mapUsers` of the `kube-system/aws-auth` ConfigMap, as EKS falls back to aws-auth per principal.
The ConfigMap is read with toolpass's own ServiceAccount and reused for a minute across users; the
ServiceAccount then needs one more rule, a Role in `kube-system`:

```yaml
rules:
  - apiGroups: [""]
    resources: ["configmaps"]
    resourceNames: ["aws-auth"]
    verbs: ["get"]
```

`toolpass probe` accepts that rule without the over-privilege warning when `eks_aws_auth` is on, and
warns when the ConfigMap is missing or unreadable.

## Resources

| Resource | Meaning |
|---|---|
| `namespace:<ns>` | a namespaced resource; add `?resource=<plural>[.<group>]&name=<n>` |
| `cluster` | a cluster-scoped resource; add `?resource=nodes&name=<n>` |
| `nonresource:<path>` | a non-resource URL such as `/metrics` or `/version` |

Examples: `namespace:payments`, `namespace:payments?resource=deployments.apps&name=api`,
`cluster?resource=nodes`, `nonresource:/metrics`.

## Actions

| Action | Kubernetes verb and resource |
|---|---|
| `raw:<verb>:<resource>[.<group>][/<subresource>]` | exactly that, e.g. `raw:create:deployments.apps`, `raw:get:pods/log`, `raw:use:podsecuritypolicies.policy` |
| `raw:<verb>` | the verb on a `nonresource:` path |
| `pods.exec` | create pods/exec |
| `pods.logs` | get pods/log |
| `pods.portforward` | create pods/portforward |
| `pods.attach` | create pods/attach |
| `scale` | update `<resource from request>/scale` |
| `secrets.read` / `secrets.list` | get / list secrets |
| `impersonate` | impersonate users (or `?resource=groups`, `serviceaccounts`) |
| `deployment.create/update/delete/restart` | create/update/delete/patch deployments.apps |
| `namespace.create/delete` | create/delete namespaces |
| `rbac.bind` | create rolebindings.rbac.authorization.k8s.io |

When both the action and the request name a resource they must agree.

## Decisions

| API server says | toolpass answers |
|---|---|
| `allowed: true` | allow |
| `allowed: false`, no `evaluationError` | deny |
| `evaluationError` set | unknown (`unsupported`) |
| 401 | unknown (`credential_rejected`): the token is invalid |
| 403 | unknown (`credential_rejected`): the ServiceAccount may not create SubjectAccessReviews |

## Probe

`toolpass probe` posts a SubjectAccessReview for a throwaway subject to prove the token may create
them, then a SelfSubjectRulesReview (any authenticated subject may) and warns when the token can do
anything beyond that one rule, listing the extra verbs and resources.

## What it cannot see

- Admission: ValidatingAdmissionPolicy, admission webhooks, Pod Security admission and resource quotas
  run after authorization and can still reject a request toolpass allowed.
- A wrong `username_template` denies everyone silently.
- Groups are whatever the caller sends, or what `groups_connection` names when the caller sends none.
  Only the `system:` namespace is protected, and only when `group_prefix` is unset.

## Managed clusters (UNVERIFIED)

- **EKS**: set `groups_connection` to an aws connection and `eks_cluster` (above). EKS access
  *policies* (`AmazonEKSClusterAdminPolicy` and the other AWS-managed policies associated with an
  access entry) are evaluated by EKS's own authorizer, not RBAC; whether a SubjectAccessReview sees
  them is unverified. A user whose access comes only from an access policy may be denied by toolpass
  and allowed by the cluster. Give access through `kubernetesGroups` and RBAC bindings to be sure.
- **GKE**: the username is the Google email; set `groups_connection` to a googleworkspace connection
  with `gke_security_group`. Whether IAM-granted access (roles/container.*) appears in a review is
  unverified.
- **AKS** with Entra: the username is the UPN (set `username_template` to match); set
  `groups_connection` to a microsoft365 connection for the group object ids. Azure RBAC for
  Kubernetes is unverified.

## Test

Unit tests run against a fake API server, including a groups source of every kind and the aws-auth
fallback. `test/kind/run.sh` creates a kind cluster, applies `test/kind/toolpass-rbac.yaml` and
`test/kind/fixtures.yaml`, and runs the end-to-end tests (`toolpass-py/tests/e2e`) against it: among
them, a connection whose `groups_connection` is a fake directory putting dana in `platform-team`, so
dana is allowed `deployment.create` with no groups sent (denied on the connection without one), and
a mapped `system:masters` group that the real API server honours. The directory halves (EKS, Google,
Entra, Okta) are tested against fakes, and against the vendors' API descriptions where one is
fetched; none against a live account.

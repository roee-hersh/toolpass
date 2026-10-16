# argocd

Argo CD has no API that asks "may user X do Y" for another user (`account/can-i` is only for the
caller). toolpass therefore reads the RBAC policy from the cluster and evaluates it locally with the
same rules as the Argo CD API server. The evaluator is a port of `argo-cd/util/rbac` and
`argo-cd/server/rbacpolicy` (v3), verified against upstream master in September 2026, and its tests
are ported from Argo CD's own.

## Credential

None of its own. An argocd connection names a kubernetes connection, whose ServiceAccount needs, in
the Argo CD namespace:

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: toolpass-argocd-reader
  namespace: argocd
rules:
  - apiGroups: [""]
    resources: ["configmaps"]
    resourceNames: ["argocd-rbac-cm", "argocd-cm"]
    verbs: ["get"]
  - apiGroups: ["argoproj.io"]
    resources: ["appprojects"]
    verbs: ["get", "list"]
```

`test/kind/argocd-rbac.yaml` has the Role and RoleBinding. All reads; nothing in Argo CD is touched.

## Connection

```yaml
  - id: argocd-prod
    integration: argocd
    kubernetes_connection: k8s-prod-eu
    namespace: argocd              # default
    rbac_configmap: argocd-rbac-cm # default
    user_subject: none             # default; or email
    # url: https://argocd.example.com   # the API server, for observed actions
```

| Key | Meaning |
|---|---|
| `kubernetes_connection` | id of the kubernetes connection for the cluster Argo CD runs in |
| `namespace` | Argo CD's namespace |
| `rbac_configmap` | name of the RBAC config map |
| `user_subject` | what Argo CD sees as the user's subject. `none`: toolpass evaluates only the default role and the caller's groups. `email`: the email is also the subject, as when your identity provider's `sub` claim is the email |
| `url` | optional: the Argo CD API server, so that the [secure-tools toolkit](#requests-a-secured-tool-makes) maps requests to it to actions; toolpass itself never calls it |

The policy (config maps and AppProjects) is cached for 30 seconds.

## How the evaluation works

Exactly as in Argo CD:

1. Policy = built-in policy (role:readonly, role:admin, admin) + `policy.csv` + every `policy.*.csv`
   key in sorted order + the AppProject's roles when the request is project scoped.
2. Matching is a glob with no separators (`*` crosses `/`) or, with `policy.matchMode: regex`, an
   unanchored RE2 regular expression, as Argo CD's Go code evaluates it. An invalid pattern never
   matches.
3. Effect: some matching `allow` and no matching `deny`.
4. Order: `policy.default` role first, then the subject, then each group that appears as the first
   element of some `g` line.
5. Group values are the caller's `groups`. When `scopes` in `argocd-rbac-cm` includes `email`, the
   email is a group value too.
6. `server.rbac.disableApplicationFineGrainedRBACInheritance` in `argocd-cm` (default true since v3):
   when true `app.update` does not imply `app.update/<resource>`; when false the top-level verb is
   checked first.
7. `server.rbac.rollback.enforce.enable` in `argocd-cm` (default false): `app.rollback` checks `sync`
   unless it is true, then it checks `rollback`.

## Resources

| Resource | Argo CD object |
|---|---|
| `applications:<project>/<name>` or `applications:<project>/<namespace>/<name>` | application |
| `applicationsets:<project>/<name>` | ApplicationSet |
| `logs:<project>/<app>`, `exec:<project>/<app>` | logs and exec (an `applications:` resource is accepted too) |
| `projects:<name>` | project |
| `clusters:<url>` or `clusters:<project>/<url>` | cluster |
| `repositories:<url>`, `write_repositories:<url>` | repository |
| `certificates:<id>`, `accounts:<name>`, `gpgkeys:<id>`, `extensions:<name>` | as named |

An application or ApplicationSet in Argo CD's own `namespace` is `<project>/<name>` to Argo CD
(`security.RBACName`), so `applications:<project>/argocd/<name>` is checked as `<project>/<name>`.

## Actions

`app.get/create/update/delete/sync/rollback/override`, `app.action/<group>/<kind>/<action>`,
`app.update/<group>/<kind>/<namespace>/<name>`, `app.delete/<group>/<kind>/<namespace>/<name>`
(the resource's name may contain `:`, as in `system:aggregate-to-edit`),
`app.update_with_get`, `app.delete_with_get`, `app.sync_with_get`, `logs.get`, `logs.get_with_app_get`, `exec.create`, `appset.get/create/update/delete`, `project.get/create/update/delete`,
`cluster.get/create/update/delete`, `repo.get/create/update/delete`,
`writerepo.get/create/update/delete`, `certificate.get/create/update/delete`,
`account.get/update`, `gpgkey.get/create/delete`, `extension.invoke`.

The `*_with_get` actions are what Argo CD checks for a patch, a delete, a sync and a pod log read:
`get` on the application first, then the action (`get` on the logs for `logs.get_with_app_get`).
Both are answered for the user; the answer is deny when either denies, unknown when either is
unknown and neither denies.

## Requests a secured tool makes

With `url` set, the [secure-tools toolkit](../guides/intercepted-actions.md) maps every request a
tool's body sends to the Argo CD REST API to the action and object Argo CD's RBAC checks, and
checks it for the session's user (`toolpass routes argocd` prints the table).

Argo CD's paths name an application but not its project, and toolpass checks applications under
their project. A request about an application must name the project: `?project=` on reads,
deletes, updates and resource changes, `project` in the body of a sync, rollback, patch or
`resource/actions/v2`. Argo CD answers "not found" when the application is in another project than
the one named, so the project in the request is the one the change lands in. A request without it
is refused.

| Request | Action, resource |
|---|---|
| `GET /api/v1/applications/<name>` and its reads (`resource-tree`, `manifests`, `events`, `resource`, `revisions/...`, ...) | `app.get`, `applications:<project>/<name>` (`<project>/<namespace>/<name>` with `appNamespace`) |
| `.../logs`, `.../pods/<pod>/logs` | `logs.get_with_app_get`, `logs:<project>/<name>` (the application's `get`, then its logs' `get`) |
| `POST /api/v1/applications`, `PUT .../<name>`, `PUT .../spec` | `app.create`, `app.update`, `app.update` |
| `PATCH .../<name>`, `DELETE .../<name>` | `app.update_with_get`, `app.delete_with_get` |
| `POST .../sync`, `DELETE .../operation`, `POST .../rollback` | `app.sync_with_get`, `app.sync`, `app.rollback` |
| `POST`/`DELETE .../resource` | `app.update/<group>/<kind>/<namespace>/<name>`, `app.delete/...` |
| `POST .../resource/actions`, `.../resource/actions/v2` | `app.action/<group>/<kind>/<action>` |
| `POST /api/v1/applicationsets` | `appset.create`, `applicationsets:<template's project>/<name>` |
| `GET`/`DELETE /api/v1/projects/<name>`, `POST /api/v1/projects`, project role tokens | `project.get`, `project.delete`, `project.create`, `project.update` |
| `POST /api/v1/clusters`, `/repositories`, `/write-repositories` | `cluster.create`, `repo.create`, `writerepo.create` on `<project>/<url>` or `<url>` from the body |
| `/api/v1/repocreds`, `/write-repocreds` (create, update, delete) | `repo.*`, `writerepo.*` on the credentials' URL |
| `GET /api/v1/account/<name>`, its tokens | `account.get`, `account.update` |

`/api/version`, `/settings`, `/session/userinfo`, `/account/can-i/...` (the agent's own) and the
login at `POST /api/v1/session` need no user permission.

Refused:

- **A second action**: a sync with `manifests`, `revision` or `revisions` (also `override`); an
  update whose body moves the application to another project (also the new project); a patch that
  may change the project or the metadata (Argo CD updates the application the patched metadata
  names, under the project it names, and reads an empty project as `default`); a create with
  `upsert` (may update instead). A JSON patch (`patchType` `json` or empty) is refused when an
  operation other than `test` writes to or moves from `/metadata...`, `/spec`, `/spec/project` or
  the whole document; a merge patch (`patchType` `merge`) when it is not an object, names
  `metadata`, sets `spec` to anything but an object, or sets `spec.project` to another value. Names
  are compared in any case, as Go's decoder reads them. Values are not looked at: a `repoURL` that
  mentions `project` is fine. Other patch types are refused.
- **Listings** (`GET /api/v1/applications`, `/projects`, `/clusters`, ...): Argo CD filters them by
  the agent's own access, not the user's. Get one object by name.
- **Objects whose project Argo CD looks up**: ApplicationSets, clusters and repositories by name.
- **Project updates**, which also need permissions on the clusters and repositories they add;
  **certificates and GPG keys**, which Argo CD checks with an empty object.
- **The agent's own account**: its password, `DELETE /api/v1/session`.
- **Anything else** under `/api`, exec (`/terminal`), proxy extensions, and the argocd CLI's gRPC-web
  calls (a `POST` to `/<service>/<method>`: a write with no route).
- **What toolpass cannot read exactly**: a body that is not JSON, a member given twice or in another
  case (Go's decoder takes either), a query Go would read differently, a name that is not a valid
  Kubernetes name.

## Decisions

| Situation | Answer |
|---|---|
| A rule allows and none denies | allow |
| No rule allows, or a rule denies | deny |
| `user_subject: none` and the policy has user-level rules toolpass cannot reach through groups | unknown (`unsupported`) rather than deny |
| A `*_with_get` action (two checks) | deny when either denies, else unknown when either is unknown |
| The policy is invalid (Argo CD would refuse it too) | unknown (`unsupported`) |
| An AppProject's role policy is invalid | evaluated without it, as Argo CD does |
| The ServiceAccount may not read the config maps or projects | unknown (`credential_rejected`) |

## What it cannot see

- Policies bound to opaque identity provider subjects (Dex `sub`, `federated_claims.user_id`).
  Use `user_subject: email` only when the subject really is the email.
- Local Argo CD accounts, project JWT tokens.
- Sync windows, project source/destination restrictions, and anything else enforced outside RBAC.
- Applications in other namespaces are addressed as `<project>/<namespace>/<name>`; toolpass does not
  look the application up to find its project.
- The route table relies on Argo CD refusing a request whose `project` is not the application's
  (`getAppEnforceRBAC`, written from Argo CD's server source; UNVERIFIED against a live server).
  Releases that predate the `project` parameter on application requests do not check it: do not set
  `url` for them.

## Test

Unit tests run against a fake API server. `toolpass-py/tests/integrations/argocd/rbac` contains the
ported Argo CD tests. `toolpass-py/tests/integrations/argocd/test_routes.py` holds the route table
to Argo CD's Swagger document (`argocd.spec` from `test/specs/fetch.sh`), its refusals, and its
resources to the check itself. `toolpass-py/tests/differential/test_argocd.py` compares the evaluator with
`argocd admin settings rbac can` when the `argocd` binary is on the PATH.

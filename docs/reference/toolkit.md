# The secure-tools toolkit: observed actions

The toolkit's declarations (`effect`, `scope`, `approve`, `credential`, the trifecta flags) are
documented in [the package README](../../toolpass-py/README.md). This page covers what was added
for actions observed at the wire: see [the guide](../guides/intercepted-actions.md) for how it
fits together.

## `Toolkit(...)` and `toolpass.configure(...)`

| Setting | Type | What it does |
|---|---|---|
| `permissions` | `Toolpass` | The engine whose connections place requests by host, and which answers the permission checks. |
| `connections` | `[{"id", "integration", "url", ...}]` | The connections to place requests by, when `permissions` is a remote engine (which knows none). Same keys as the config file, without secrets. |
| `private` | iterable of connection ids | Reads from these connections count as reading private data (the exfiltration guard). |
| `entitlements` | path, mapping or `Entitlements` | The recorded actions the tools are held to. |
| `record` | path | Write every call's allowed actions to this file (`ActionRecorder`). |
| `strict` | bool | Refuse requests to unknown hosts, and tools without entitlements when a file is given. |
| `intercept` | bool, default True | Turn the hook off for this toolkit. |

## `secured_tool(...)`

- `effect` is now optional. Without it, the tool's effect is the strongest recorded one (with
  entitlements), else unknown: its requests are classified one by one and `limits` count each.
- `resources={"repo": "acme/gitops-*"}` fences the resources the body's requests touch, by resource
  type, with the same globs as `scope`.

## `as_agent()`

A context manager for a block of the body that acts on the agent's own authority: the permission
check against the session's user is skipped for requests made inside it, nothing else. No effect
outside a tool body (a warning is logged).

## `ActionRefused`

A `ToolRefused` raised from inside the client library when a request the body made is refused.
`action` is the `ObservedAction`. Codes, beyond the toolkit's own:

| Code | Meaning |
|---|---|
| `unknown_host` | The host is not a configured connection (strict), or not among an entitled tool's recorded hosts. |
| `unmapped_action` | A write on a known host that no route describes. |
| `unresolved_resource` | The route needs a resource the request does not name (a Slack channel by name). |
| `unentitled_action` | The action is not among the tool's recorded actions. |
| `effect_exceeded` | A request stronger than the tool's declared effect (a write from a `read` tool). |
| `effect_mismatch` | A declared effect weaker than a recorded action (refused before the body). |
| `no_entitlements` | A strict toolkit with an entitlements file, and this tool is not in it. |

## `ObservedAction`

What one request turned out to be; `AuditEvent.actions` carries one dict per request
(`to_dict()`):

| Field | Meaning |
|---|---|
| `method`, `host`, `path` | The request. |
| `connection`, `integration` | Where toolpass placed it; absent for an unknown host. |
| `action` | `integration:action`, e.g. `github:pr.create`; absent when no user permission applies. |
| `resource` | The engine's resource, e.g. `repo:acme/x`. |
| `effect` | `read`, `write` or `destructive`. |
| `as_agent`, `sends_out`, `untrusted_output` | Present when true. |
| `outcome` | `allowed`, `refused` or `pending`; `code` and `reason` when not allowed. |
| `note` | `unknown_host` or `unmapped` for a request toolpass could not place or route. |

## Entitlements

`Entitlements.load(path)`, `Entitlements.from_mapping(doc)`, `.for_tool(name)`;
`ToolEntitlements(tool, actions, hosts)`; `Entitlement(action, resource, as_, effect, approve)`.
The file:

```yaml
tools:
  <tool name>:
    hosts: [<host the body may call besides the connections>]
    actions:
      - action: <integration>:<action>
        resource: <type>:<id, template over the arguments, or glob>
        as: user | agent
        effect: read | write | destructive
        approve: false
```

A template (`repo:{repo}`) names the tool's arguments; it is filled from them and compared
exactly, or as a glob when the template's own text contains `*` (an argument value is never a
glob). A template whose argument the call does not have covers nothing. `ActionRecorder(path).record(tool, arguments, observed)` merges one call's allowed actions
into the file, atomically.

## Routes

`toolpass.core.routes`: `Route(method, path, action, resource, effect, sends_out,
untrusted_output, body)`, `RouteTable(integration, routes, claims, unrouted, prefixes, locate)`,
`Request`, `Observed`. An integration exposes its table from `Integration.routes()`;
`toolpass routes [INTEGRATION]` prints it. Path templates: `{name}` is one segment, `{name+}` the
rest of the path, `**` anything; the most specific template whose route takes the method wins.

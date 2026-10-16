# The secure-tools toolkit: observed actions

The toolkit's declarations (`effect`, `scope`, `credential`, the trifecta flags) are
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

## `toolpass.tool(...)`

`secured_tool` is the same decorator under its earlier name (toolpass 0.11 and before); it stays for two releases.

- `effect` is now optional. Without it, the tool's effect is the strongest recorded one (with
  entitlements), else unknown: its requests are classified one by one and `limits` count each.
- `resources={"repo": "acme/gitops-*"}` fences the resources the body's requests touch, by resource
  type, with the same globs as `scope`.

## `as_agent()`

A context manager for a block of the body that acts on the agent's own authority: the permission
check against the session's user is skipped for requests made inside it, nothing else. No effect
outside a tool body (a warning is logged).

## `session_from_header(app, *, key=None)`

Wraps an ASGI app, an MCP server's Streamable HTTP app, so each request runs under a `Session` for
the user in its `X-Toolpass-User` header, which the agent sets from the login it already did, with
the groups of `X-Toolpass-Groups` (comma-separated) when sent. The requests of one conversation
share one `Session`: the one the agent names in `X-Toolpass-Session`, else the MCP session
(`Mcp-Session-Id`, kept until its DELETE), else the request alone; the wrapper remembers 4096. No
user header: no session, even when the process made one current, and a toolpass tool refuses the
call. An empty or doubled header is refused with 400. With `key`, a request must carry the same
secret in `X-Toolpass-Key` or is refused with 403; use it when others than the agent can reach the
server; without it, a request from another machine is refused with 403. A request with an
`Authorization` header is refused with 400 when it also names a user. A websocket connection is
held to the same rules (close codes 4403 and 4400).

## `toolpass.mcp.token_session(user_claim="email", groups_claim=None)`

A session source (`Toolkit(session=...)`, `toolpass.configure(session=...)`) for an MCP server whose
callers sign in with their own token, verified by the `mcp` SDK's auth: each call runs under a
`Session` for the token's user (its `email` claim, or `sub`; a token with `email_verified` false
names no one), with the groups of `groups_claim`. The calls made with one token share one
`Session`. A request without a verified token has no session, so a toolpass tool refuses it.
`toolpass.USER_HEADER`, `toolpass.GROUPS_HEADER`, `toolpass.SESSION_HEADER` and
`toolpass.KEY_HEADER` are the header names.

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
| `outcome` | `allowed` or `refused`; `code` and `reason` when refused. |
| `note` | `unknown_host` or `unmapped` for a request toolpass could not place or route. |

## Entitlements

`Entitlements.load(path)`, `Entitlements.from_mapping(doc)`, `.for_tool(name)`;
`ToolEntitlements(tool, actions, hosts)`; `Entitlement(action, resource, as_, effect)`.
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
```

An action with `approve: true`, from before approval was removed, makes the file fail to load, so
a tool is never run without the confirmation its file asked for. `approve: false` is ignored.

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

## `toolpass.otel.audit_sink(tracer=None, *, then=None)`

An audit sink (`Toolkit(audit=...)`, `toolpass.configure(audit=...)`) that records each run as an
OpenTelemetry span, `toolpass.tool <name>`, with the user, outcome, code and reason as attributes
and each request the body made as a `toolpass.request` span event; a refused or failed run has
status ERROR. Arguments are not exported. It uses the global tracer provider, or `tracer`; `then`
is a sink to hand each event on to as well. Needs `toolpass[otel]`. See
[the dashboard guide](../guides/dashboard.md#export-to-opentelemetry).

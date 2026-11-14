# HTTP API reference

The toolpass server (`toolpass serve`) answers permission checks (`POST /check`), collects the runs
agents report (`POST /runs`), serves the dashboard over them (`GET /dashboard/`) and answers
permission questions to MCP clients (`POST /mcp`). Clients: `Toolpass.remote` in the
[`toolpass` package](client.md), [`toolpass-client`](client.md#toolpass-client-node) for Node, or any
HTTP client. The in-process engine answers with the same decisions and codes, without HTTP.

## POST /check

Send the configured `api_key` as a bearer token and a JSON body of at most 64 KiB. Unknown fields
are rejected, and so is a field given twice; names match regardless of ASCII case. The body is framed
by one `Content-Length` or by `Transfer-Encoding: chunked`, never both or twice. `user` is an email
of at most 320 bytes, with no whitespace or control characters (C1 and Unicode separators included)
and none of the letters that case-fold to ASCII ones (`İ`, `ı`, `ſ`, the Kelvin sign). `groups` holds
at most 200 entries of at most 256 bytes each. `token`, optional, is the user's own ID token, for a
connection that reads the groups from it ([Groups from the user's token](../guides/groups.md)):
printable ASCII without spaces, at most 16 KiB; it is never logged, and the caches key on a digest
of it.

```json
{
  "user": "dana@example.com",
  "groups": ["platform-team"],
  "connection": "k8s-prod-eu",
  "action": "deployment.create",
  "resource": "namespace:payments"
}
```

```json
{ "decision": "allow" | "deny" | "unknown", "reason": "<code>: <text>", "identity": "<account>" }
```

`identity` is the account the user's email resolved to in the system (a GitHub login, a Jira
account id), present whenever one was resolved, allow or deny alike; it is absent when the email
did not resolve (`user_not_found`, a lookup that failed) and on requests refused before a lookup.

| Case | HTTP | decision | code |
|---|---|---|---|
| Evaluated | 200 | allow / deny | `allowed` / `denied` |
| User has no account in that system | 200 | deny | `user_not_found` |
| Several accounts match the email | 200 | unknown | `user_ambiguous` |
| Upstream timeout, error or rate limit | 200 | unknown | `upstream_timeout`, `upstream_error`, `upstream_rate_limited` |
| toolpass's own credential was rejected | 200 | unknown | `credential_rejected` |
| Resource not visible to toolpass | 200 | unknown | `resource_not_visible` |
| Policy construct toolpass does not understand | 200 | unknown | `unsupported` |
| The connection cannot ask as configured (no identity source that works): nothing was checked | 200 | unknown | `not_configured` |
| Bad request, unknown connection or action | 400 | unknown | `invalid_request`, `unknown_connection`, `unknown_action` |
| Bad or missing API key | 401 | unknown | `unauthorized` |
| A method other than `POST` | 405 | unknown | `invalid_request` |
| A body over 64 KiB | 413 | unknown | `invalid_request` |

`deny` means the third-party system positively said no. Anything toolpass could not evaluate is
`unknown`. Callers should treat `unknown` as deny.

## Fresh checks

The request also accepts `"fresh": true`. A fresh check skips every cache for that one request,
the decision cache, the identity cache and the lookups an integration caches itself (role
definitions, policies), and asks the upstream system now; what it learns replaces the cached
entries, so reads keep using the cache. Use it for destructive actions (delete, merge, scale),
where a 30-second-old answer is not good enough, and not for reads: a fresh check costs every
upstream call an uncached check makes (for Argo CD, the policy config maps and the project list;
for Vault, the policies involved; for Snowflake, the grants of every role in the hierarchy).
Lookups that are not permission state are reused for as long as any check reuses them: GitHub's
organization-wide SAML identity listing (the permission read after it is live anyway), AWS
Identity Center's role inventory and permission set names, Salesforce's object and field
describes, and toolpass's own principal name in Databricks. Fresh checks share
a lookup one of them has in flight (an identity, a role's permissions, a policy) and one read
less than a second ago; a fresh answer is one from reads in flight when the caller asked or begun
no more than a second before. Maps toolpass reads from a local file
(`role_map_file`, `user_map_file`) are re-read on their own schedule, not per fresh check. A fresh
check narrows the window between the check and the action to the time between the two; it does
not close it. Closing it needs a conditional write in the upstream system (for example `If-Match`
with an ETag), which only some APIs support. A toolpass built before `fresh` existed rejects a
request that carries it (`invalid_request: unknown field "fresh"`, which the clients report as
`unknown`), so upgrade the server before turning it on in agents.

## GET /healthz

Returns `{"status":"ok"}` with status 200, without authentication. It says the process is up, not
that every connection works; `toolpass probe` checks connections.

## POST /runs

The runs of toolpass tools an agent reports, for [the dashboard](../guides/dashboard.md). Send the
`api_key` as a bearer token and a JSON body of at most 1 MiB: the reporting process's name and 1
to 500 runs, each an audit event (`AuditEvent.to_dict()`: at least a string `tool` and `outcome`).
A run larger than 64 KiB keeps its outcome and loses its `actions` and `arguments`.

```json
{"server": "shop", "runs": [{"time": "...", "tool": "open_pr", "outcome": "refused", "user": "sam@acme.dev", "actions": [...]}]}
```

`202 {"accepted": N}`; `400` with `{"error": "..."}` for a body that is not that; `401` without the
key; `413` over the size. The server keeps the latest 10,000 runs in memory; a toolkit sends them
itself (see the guide), so nothing calls this by hand.

## GET /dashboard/

The dashboard page, with `watch.js` and `watch.css` beside it, served to anyone: it holds no data.
`GET /dashboard/events?since=N` returns the runs after sequence `N` (`0`: the latest 500), as
`{"kind": "dashboard", "events": [...], "next": M}`, to holders of the `api_key` as a bearer token
(`401` otherwise); the page asks for the key and keeps it in the browser tab.

## POST /mcp

The permission questions as [MCP](https://modelcontextprotocol.io) tools, for an assistant
([Ask who may do what, from your assistant](../guides/ask.md)). Send the `api_key` as a bearer
token (`401` otherwise, for every method) and one JSON-RPC 2.0 message of at most 64 KiB (`413`
over it). A request with an `Origin` header that does not name the host it was sent to (its `Host`
header) is `403`: a browser sends one for a page from another site, and MCP clients that are not
browsers send none.

This is the Streamable HTTP transport, answered with JSON only. A request is answered with one
`application/json` object and status 200; a notification or a response is accepted with `202` and
no body. There is no stream and no session: `GET` and `DELETE` answer `405` with `Allow: POST`,
no `Mcp-Session-Id` is issued, and nothing is kept between requests. A body that is not one
message (unparseable JSON, `NaN` or `Infinity`, a batch, no `jsonrpc: "2.0"`, a field given twice
anywhere in it) is
`400` with a JSON-RPC error.

The protocol revisions `2025-11-25`, `2025-06-18` and `2025-03-26` are served, the ones that begin
with `initialize`. `initialize` answers with the revision the client asked for when it is one of
those, and `2025-11-25` otherwise. A request whose `MCP-Protocol-Version` header names any other
revision is `400` with error `-32022` and `data.supported`, the list; a client that speaks a newer
revision first (the `server/discover` probe of `2026-07-28`) reads that and falls back to
`initialize`.

| Method | Result |
|---|---|
| `initialize` | the revision, `capabilities: {"tools": {}}`, `serverInfo` and `instructions` for the model |
| `ping` | `{}` |
| `tools/list` | the three tools, each with an input and an output schema and `readOnlyHint: true` |
| `tools/call` | the tool's result, as `structuredContent` and as the same JSON in a text block |
| anything else | error `-32601` |

| Tool | Arguments | `structuredContent` |
|---|---|---|
| `list_connections` | none | `{"connections": [{"id", "integration"}]}` |
| `list_actions` | `connection` | `{"connection", "integration", "actions": [{"name", "description", "pattern"}]}`; `pattern: true` marks a shape such as `raw:<verb>:<resource>` |
| `check_permission` | `user`, `connection`, `action`, `resource`, and optionally `groups` and `fresh`, as in `POST /check` | `{"decision", "reason", "code", "identity"}` |

`check_permission` runs the check `POST /check` runs, with the same validation, caches and
decision log line; it does not take `token`. A decision is a result with `isError: false`, `unknown`
included. A check that never reached the system (`invalid_request`, `unknown_connection`,
`unknown_action`) has `isError: true` and the same content. Arguments that do not fit the tool's
schema (one missing, one of the wrong type, one the tool does not take) and a `list_actions` for a
connection that does not exist are `isError: true` with a sentence and no `structuredContent`. A
tool that does not exist is error `-32602`.

# Python API reference

The `agent-authorizer` package (`pip install agent-authorizer`, Python 3.10 or later), and the Node client
`agent-authorizer-client` at the end. For how to use them in an agent, see the
[agent tools guide](../guides/agent-tools.md).

## AgentAuthorizer

Three ways to make one, all with the same methods:

```python
import agent_authorizer
from agent_authorizer import AgentAuthorizer

tp = AgentAuthorizer.from_config("agent-authorizer.yaml")                   # the engine in this process
tp = AgentAuthorizer(connections=[{"id": "demo", "integration": "fake", "users": "dana@example.com"}])
tp = AgentAuthorizer.remote("https://agent-authorizer.internal", api_key)   # an agent-authorizer server
```

| Constructor | What it is |
|---|---|
| `AgentAuthorizer.from_config(path, *, logger=None)` | The engine in this process, from an agent-authorizer YAML file. The file's `decision_cache_seconds`, `identity_cache_seconds` and `decision_log` apply; `listen` and `api_key` are not needed |
| `AgentAuthorizer(connections, *, decision_cache_seconds=30, identity_cache_seconds=900, decision_log="none", logger=None)` | The same, with the connections in code: mappings with exactly the keys of the file ([configuration](configuration.md#connections-in-code)) |
| `AgentAuthorizer.remote(url=None, api_key=None, timeout=10.0)` | A client for a running agent-authorizer server. `url` defaults to `$AGENT_AUTHORIZER_URL` or `http://localhost:8080`, `api_key` to `$AGENT_AUTHORIZER_API_KEY`, and a missing key raises `ValueError` |

`logger` takes a `logging.Logger` for the engine's own log lines (the stdlib `agent_authorizer` logger by
default). `AgentAuthorizer()` with no connections raises `TypeError`. Build one per process and share it:
each in-process `AgentAuthorizer` keeps its own caches.

For `remote`, the URL must be `https://`, or `http://` on `localhost` or a loopback address,
because the API key travels in a header. Redirects are never followed. `timeout` is the seconds to
wait for the connection and then for each read; the server waits up to the connection's `timeout`
(8 s by default) for the upstream, so keep this a little above that.

## check, allowed, require

```python
d = tp.check("dana@example.com", "jira-main", "DELETE_ISSUES", "issue:PAY-123")
d.decision  # "allow", "deny" or "unknown"
d.reason    # "denied: Dana Levi does not hold DELETE_ISSUES on issue PAY-123"
d.code      # "denied"
d.allowed   # True only for allow
d.status    # the HTTP status the server used (in-process: the one it would use); 0 if none arrived
d.identity  # the account the email resolved to (a GitHub login, a Jira account id); "" if none

tp.allowed(...)          # True or False
tp.require(...)          # returns the decision on allow, raises PermissionDenied otherwise
await tp.acheck(...)     # check for async code; the lookup runs in a worker thread
await tp.arequire(...)   # require for async code
```

All five take `(user, connection, action, resource, groups=None, *, fresh=False, token=None)`:

| Argument | Meaning |
|---|---|
| `groups` | A list of the user's groups, for systems that grant by group, such as Kubernetes (a kubernetes connection with `groups_connection` looks them up when none are sent) |
| `fresh` | Skip agent-authorizer's caches and ask the system now ([fresh checks](api.md#fresh-checks)) |
| `token` | The user's own ID token, for a connection that reads the groups from it ([Groups from the user's token](../guides/groups.md)) |

`check` never raises for a failed lookup: every failure is an `unknown` decision. With `remote`, a
connection error, a timeout, a redirect, a body that is not a decision, or an `allow` with a
non-200 status all become `unknown` with the code `client_error`. `PermissionDenied` carries the
`decision`, `user`, `connection`, `action` and `resource`, and its text names all of them.

In-process only:

| | |
|---|---|
| `tp.probe()` | Verify every connection's credential: a list of `(connection id, ok, summary or error)` |
| `tp.connections()` | The configured connection ids |
| `tp.local` | `True` when the engine runs in this process (also available on `remote`) |

## guarded

Wraps a function so its body runs only after agent-authorizer said `allow`.

```python
from agent_authorizer import guarded

@guarded(tp, "jira-main", "DELETE_ISSUES", "issue:{key}", user=current_user, fresh=True)
def delete_issue(key: str) -> str: ...
```

| Parameter | Meaning |
|---|---|
| `connection`, `action` | The connection id and one of its actions (`agent-authorizer catalog <integration>` lists them) |
| `resource` | A format string over the call's arguments, such as `"issue:{key}"`; a parameter left at its default is available too. A call that cannot fill it checks nothing and runs nothing |
| `user` | A string, a zero-argument callable, or a `ContextVar` your application sets. Read on every call, never from the arguments |
| `groups` | The user's groups, from the same kinds of source; it must yield a list |
| `deny` | Optional. Called with the `PermissionDenied`; its return value is returned instead of raising |
| `fresh` | Optional. Every check skips agent-authorizer's caches |

What it guarantees:

- The wrapped function keeps its exact signature, so no `user` field appears in a tool schema. A
  stray `user` keyword argument is a `TypeError` before any check; a `user` key inside a dict of
  arguments (the Claude Agent SDK shape) is ignored.
- Anything but `allow` stops the call before the body runs.
- A function with ordinary parameters is called with keyword arguments (LangChain, Strands, MCP); a
  function whose one parameter is a dict gets all the arguments in it (the Claude Agent SDK handler
  shape, `async def f(args: dict)`). `async def` works, and the check runs in a worker thread.

`agent_authorizer.current(source)` resolves a user or groups source the same way, for code outside a
guarded function. A `ContextVar` with nothing set raises a `RuntimeError` that names it.

## Framework adapters

Each adapter module needs its extra (`pip install "agent-authorizer[<extra>]"`) and exports `Rule` along
with the adapter. All take `tp`, then `rules`: a mapping of tool name to
`Rule(connection, action, resource, fresh=False)`, a `(connection, action, resource)` tuple, or
`None` (the tool runs unchecked even under `strict`); and `strict=False` (refuse tools that have
no rule).

| Module (extra) | Adapter | User from | Other arguments |
|---|---|---|---|
| `agent_authorizer.strands` (`strands`) | `AgentAuthorizerAuthorization`, an intervention handler | `invocation_state[user_key]` | `user_key="user_id"`, `groups_key=None` |
| `agent_authorizer.langchain` (`langchain`) | `AgentAuthorizerMiddleware`, agent middleware; `.tool_node(tools)` for LangGraph | the runtime context's `user_key`, else `user` | `user`, `groups`, `user_key="user_id"`, `groups_key=None` |
| `agent_authorizer.openai_agents` (`openai-agents`) | `AgentAuthorizerGuardrails`; `.apply(agent)`, `.protect(tools)` | the run context's `user_key`, unless `user` | `user_key="user_id"`, `groups_key=None`, `user`, `groups` |
| `agent_authorizer.claude_agent_sdk` (`claude-agent-sdk`) | `AgentAuthorizerHooks`; `.apply(options)`, `.hooks()`, `.can_use_tool` | `user` (required) | `groups`, `timeout=60` |
| `agent_authorizer.google_adk` (`google-adk`) | `AgentAuthorizerCallbacks`; `.apply(agent)`, `.plugin()` | the session's `user_id`, unless `user` | `user`, `groups` |
| `agent_authorizer.crewai` (`crewai`) | `AgentAuthorizerHooks`; `.register()`, `.unregister()`, or `with` | `user`, else the kickoff input `user_input` | `user`, `groups`, `user_input="user_id"`, `groups_input=None` |
| `agent_authorizer.pydantic_ai` (`pydantic-ai`) | `AgentAuthorizerAuthorization`, a capability; `AgentAuthorizerToolset(toolset, tp, rules)` | `user`, else `deps.user` | `user`, `groups` (else `deps.groups`) |
| `agent_authorizer.llamaindex` (`llamaindex`) | `AgentAuthorizerAuthorization`; `.wrap(tools)` | `user` (required) | `groups` |

Each refuses a call when the user is missing, when a resource field is not exactly the string or
integer the tool declares, when a field is missing with no default, on any answer but `allow`,
and on any error inside the check; the model reads `agent-authorizer refused this call: <reason>` as the
tool's result. The adapter's docstring says how its framework delivers that result and what it
cannot check.

## The write log line

agent-authorizer never sees the write itself, so after a guarded body runs, `guarded` logs one line on the
`agent_authorizer` logger at INFO:

```text
unconditional write: dana@example.com ran DELETE_ISSUES on issue:PAY-123 in jira-main;
agent-authorizer said allow (allowed: dana may delete issues in PAY) at 2026-09-24T10:00:00.412+00:00,
fresh=True; the write was not conditioned on the state agent-authorizer saw (no If-Match),
so check and write were not atomic
```

A refused call logs nothing. A body that raises still logs, since the write may have happened. The
adapters log the same line after a checked tool runs, with `raised <error> from` or `got an error
result from` in place of `ran` when the tool failed.

## Fresh checks and atomicity

A fresh check narrows the gap between the check and the action; it does not close it. Closing it
needs a conditional write in the upstream system, such as `If-Match` with an ETag. An agent-authorizer
server older than the `fresh` field rejects it, which `remote` reports as `unknown`, so upgrade the
server before turning `fresh` on. The [API reference](api.md#fresh-checks) has the details.

## Moving from toolpass-client (Python)

`toolpass-client` on PyPI, the old Python client from when the project was called toolpass, is replaced by this package and gets no new
releases; installed versions keep working. To move, depend on `agent-authorizer` and change
`from toolpass_client import Toolpass` / `Toolpass()` to `from agent_authorizer import AgentAuthorizer` /
`AgentAuthorizer.remote()` (and `toolpass_client.strands` to `agent_authorizer.strands`).

## agent-authorizer-client (Node)

The Node and TypeScript client of an agent-authorizer server (`npm install agent-authorizer-client`, Node 18.17 or
later, no dependencies). It follows the same rules as `AgentAuthorizer.remote`.

```ts
import { AgentAuthorizer, guarded } from "agent-authorizer-client";

const tp = new AgentAuthorizer(); // AGENT_AUTHORIZER_URL and AGENT_AUTHORIZER_API_KEY; or new AgentAuthorizer({ url, apiKey, timeoutMs })

const d = await tp.check("dana@example.com", "jira-main", "DELETE_ISSUES", "issue:PAY-123", { groups, fresh: true });
await tp.require(...); // rejects with PermissionDenied unless allow

const deleteIssue = guarded(tp, "jira-main", "DELETE_ISSUES", "issue:{key}", { user, fresh: true })(
  async ({ key }: { key: string }) => { ... },
);
```

`check`, `allowed` and `require` return promises. The wrapped function takes one object of
arguments, which is what every Node agent framework passes; `user` and `groups` are a string, a
zero-argument function or an `AsyncLocalStorage`; `deny` works as in Python. The Node client does
not log the write line.

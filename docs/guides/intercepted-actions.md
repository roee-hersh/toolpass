# Check what a tool does, not what it says

A secured tool used to be safe to the extent its declaration was right: `effect="write"`,
`authorize=permission_check(tp, "github-acme", "pr.create", "repo:{repo}")`. The declaration is a
claim. If the body also deletes a branch, nothing notices; and writing it correctly means knowing
GitHub's permission model.

toolpass can instead watch the requests a tool's body makes. Every HTTP request that leaves the
process while a secured tool runs is mapped to the integration action and resource the permission
engine already answers for (`POST /repos/acme/x/pulls` is `pr.create` on `repo:acme/x`), checked,
and refused before it is sent when the session's user may not do it. The tool declares nothing:

```python
import toolpass
from toolpass import Session, Toolpass, as_agent, secured_tool
from github import Github

tp = Toolpass.from_config("toolpass.yaml")       # the github-acme connection, among others
toolpass.configure(permissions=tp)


@secured_tool()
def propose_fix(repo: str, path: str, fix: str) -> str:
    gh = Github(auth=token)
    with as_agent():                             # the agent may read on its own authority
        current = gh.get_repo(repo).get_contents(path)
    pr = gh.get_repo(repo).create_pull(title="fix", body=fix, head="fix", base="main")   # checked against the user
    return f"opened #{pr.number}"


with Session("dana@example.com").active():
    propose_fix("acme/x", "README.md", "...")
```

When Dana may not open pull requests in `acme/x`, the `POST` never reaches GitHub: the hook raises
`ActionRefused` (a `ToolRefused` with code `not_authorized`) inside PyGithub's call, the body
unwinds, and the model reads the refusal. The audit event lists both requests as `actions`, the
read allowed and the pull request refused.

## How a request is understood

1. **Which system.** The request's host is matched against the connections of the `permissions`
   engine (`Toolpass.from_config` or `Toolpass(connections=[...])`): `api.github.com` or a GitHub
   Enterprise Server URL is the `github-acme` connection, `slack.com` is `slack-main`. A remote
   engine knows no connections; name them with `connections=[{"id": ..., "integration": ...,
   "url": ...}]` then.
2. **Which action.** The integration's route table maps the method and path (and, where the path
   does not carry it, the body: Slack's `channel`) to an action, a resource and an effect.
   `toolpass routes github` prints the table.
3. **The checks**, in the toolkit's usual order: the tool's `resources=` scope, limits, the
   permission check of the session's user, the untrusted-input check on the outgoing URL and
   body, the exfiltration guard for routes that send data out.
4. **The verdict.** Allowed requests go out. A refused request raises from inside the client
   library; the call's outcome is the refusal even when the body catches the exception and returns
   something, so a half-result never reaches the model.

A request the toolkit cannot place is handled by what it knows:

| Request | Default | With `strict=True` |
|---|---|---|
| To a host that is not a configured connection | allowed, audited `unknown_host` | refused |
| A read on a known host with no route | allowed as `read`, audited `unmapped` | the same |
| A write on a known host with no route | refused `unmapped_action` | the same |
| A route whose resource the request does not name (a Slack channel by name) | refused `unresolved_resource` | the same |
| From a thread that lost the call's context | allowed, unchecked (see below) | the same |

Requests about the agent's own identity (`GET /user`, `auth.test`), search and rate limits need no
user permission: they are allowed and audited with no action.

## `as_agent()`

Some of a body runs rightly on the agent's own authority: it reads a file the user cannot, then
opens a pull request the user must be allowed to open. `as_agent()` marks that block. Inside it the
permission check against the session's user is skipped and nothing else: scope, limits, the
untrusted-input and exfiltration checks and entitlements still apply, and each action is audited
with `as_agent`. The block covers only requests made inside it, not a nested secured tool. Keep it
narrow; a whole body inside `as_agent()` is a tool that acts as the agent for everyone.

## Record what a tool does, then hold it to that

A refusal at the wire lands mid-body: a tool that created a branch, pushed and then had its pull
request refused leaves the branch behind. Recorded entitlements move the refusal before the body:

```python
toolpass.configure(permissions=tp, record="toolpass-tools.yaml")     # in development or tests
```

Every call writes the actions its body performed to the file, with argument values turned back
into templates:

```yaml
tools:
  propose_fix:
    actions:
    - action: github:repo.read
      resource: repo:{repo}
      as: agent
      effect: read
    - action: github:pr.create
      resource: repo:{repo}
      as: user
      effect: write
```

In production, the file is what the tool may do:

```python
toolpass.configure(permissions=tp, entitlements="toolpass-tools.yaml")
```

Before the body runs, every `as: user` action whose resource fills from the arguments is checked
against the engine, and limits are taken for the strongest recorded effect. The hook stays on as the backstop: a request
that matches no recorded action is refused `unentitled_action`, a host the file does not list is
refused `unknown_host`, and a tool that declares `effect="read"` while its recorded actions include
a write is refused `effect_mismatch`. A resource known only at run time (an id fetched by an
earlier request) is written as a glob (`repo:acme/*`) or a literal and checked at the wire.

`strict=True` also refuses a tool that has no entry in the file.

## Through the frameworks

The hook sits under the tool body, so it works wherever the toolkit works: the tests drive the
same scenario (a tool that declares nothing, a read as the agent, a refused pull request) through
each of the nine supported frameworks' own tool execution path, with a scripted model: the real
agent loop for eight of them, and for the Claude Agent SDK its in-process MCP server, which is what
runs a tool when the agent calls it. In every one the call's context reaches the body, the refused
request never leaves the process, and the allowed one does. What differs is how each framework hands
a tool's exception to the model. Where it masks or re-raises it, set
`toolpass.configure(on_refuse=str)` so the refusal is the tool's result and the model reads why:

| Framework | Without `on_refuse=str` | Needs `on_refuse=str` |
|---|---|---|
| Strands Agents | the model reads `Error: ActionRefused - <reason>` | no |
| LlamaIndex | the model reads the reason as an error tool output | no |
| Claude Agent SDK (a tool on its in-process MCP server) | the server returns the reason as an error result | no |
| LangChain `create_agent`, and LangGraph's prebuilt `ToolNode` with its default error handler | the exception escapes the run | yes |
| OpenAI Agents SDK (`function_tool`) | "An error occurred while running the tool" | yes |
| Pydantic AI | the exception escapes the run | yes |
| Google ADK | the run ends on the exception | yes |
| CrewAI | the model reads CrewAI's own wording of the error | yes |
| MCP server (the `mcp` SDK) | "Error executing tool <name>", the message masked | yes |

With `on_refuse=str` the refusal arrives as an ordinary tool result in every framework.

## What it cannot see

- A client library the hook does not patch (urllib3 and everything on it, the standard library,
  httpx and aiohttp are covered; gRPC is not yet), or a custom httpx transport.
- A request made from a thread the SDK started (boto3's managed transfers, pubsub's `publish()`,
  Azure's pollers): the call's context does not reach it, so it is not attributed to the call and
  not checked. toolpass cannot tell such a request from the application's own traffic in another
  thread, so it does not refuse it either. `TransferConfig(use_threads=False)` keeps boto3's
  transfers in the caller's thread; `contextvars.copy_context().run(...)` carries the context into
  a thread you start yourself.
- The response: toolpass does not read what comes back. A read from an untrusted route marks the
  session as having seen untrusted content; the tool's return value is recorded and fenced as
  always.
- Meaning in a body it cannot parse: a GraphQL mutation or an SQL statement is refused until a route
  reads it.

The hook is on for a toolkit that has `permissions`, `connections`, `entitlements`, `record` or
`strict` set, and for any tool that declares no `effect`. `Toolkit(intercept=False)` turns it off.
A tool with a declared effect in a toolkit with none of those settings runs as before.

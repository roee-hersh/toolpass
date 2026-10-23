# toolpass

**A pass for every tool call: secure-by-default tools for AI agents, under any agent framework.**

> The secure-tools API (`Toolkit`, `Session`) is alpha and may still change; the permission
> engine (`Toolpass`, `guarded`, the adapters) is stable.

The toolpass server's dashboard (`/dashboard/`) shows every run of your toolpass tools, from every
agent that reports to it: who it ran for, and every request each run made inside GitHub, Jira,
Slack and the other systems, allowed or refused. A toolkit whose permissions are remote reports
with nothing else configured; `toolpass.otel` exports each run as a span. See
[the dashboard guide](../docs/guides/dashboard.md), and `python examples/demo/run.py` for five
systems at once.

Teams write custom tools for their own agents: open a pull request against the GitOps repository,
scale a deployment, read customer records, post to Slack. Those tools usually run with one service
credential for everyone who talks to the agent, and every team rebuilds the same safety checks
by hand, or skips them. toolpass is a decorator on a plain Python function. The function declares
nothing; toolpass watches what it does:

```python
import toolpass
from toolpass import Session, Toolpass, as_user
from github import Github                            # any SDK: PyGithub, slack_sdk, boto3, raw requests

toolpass.configure(permissions=Toolpass.from_config("toolpass.yaml"))   # the github-acme connection, among others


@toolpass.tool()
def propose_fix(repo: str, path: str, fix: str) -> str:
    repo_ = Github(auth=token).get_repo(repo)
    current = repo_.get_contents(path)               # the agent reads on its own authority
    with as_user():                                  # pr.create on repo:acme/x: checked against the user
        pr = repo_.create_pull(title="fix", body=fix, head="fix", base="main")
    return pr.html_url


with Session("dana@example.com").active():           # the user your app authenticated
    agent.run(prompt)
```

Every HTTP request the body makes is mapped to the action it is, checked against the session's
user, and refused before it is sent when they may not do it; see
[Actions observed at the wire](#actions-observed-at-the-wire). A tool can also declare what it
does, and the two compose:

```python
from toolpass import permission_check

toolpass.configure(credentials={"github-bot": github_token}, limits={"destructive": 3})


@toolpass.tool(
    effect="write",
    scope={"repo": "acme/gitops-*"},                 # the tool itself can touch nothing else
    authorize=permission_check(tp, "github-acme", "pr.create", "repo:{repo}"),
    credential="github-bot",                          # injected below; the model never sees it
)
def open_gitops_pr(repo: str, title: str, change: str, *, credential: str) -> str:
    ...
```

The decorated function keeps its signature (minus the injected `credential`), so LangChain,
Pydantic AI, CrewAI, the OpenAI Agents SDK or your own loop can put their own `@tool` on top. No
adapter is needed. Or let `toolpass.tool` apply it: `as_tool=` takes the framework's decorator and
returns that framework's tool, with every check inside.

```python
from langchain_core.tools import tool

@toolpass.tool(effect="read", untrusted_output=True, as_tool=tool)
def read_email(id: str) -> str: ...
```

Some frameworks hide a tool's exception from the model or let it escape the run (the OpenAI Agents
SDK's `function_tool` answers "An error occurred while running the tool"; LangChain's default
`ToolNode`, Pydantic AI, Google ADK, CrewAI and the MCP SDK's server behave their own way). There,
`toolpass.configure(on_refuse=str)` makes a refusal the tool's result, so the model can tell the user
why; [the guide](../docs/guides/intercepted-actions.md#through-the-frameworks) lists which
frameworks need it.

`toolpass.configure(...)` sets up the default behind `toolpass.tool`; tools declared earlier pick the
settings up at call time. A program that needs two sets of settings makes `Toolkit(...)` objects
and uses their `toolpass.tool`.

## What a tool can declare

| Protection | Declared with | What it guarantees |
|---|---|---|
| User from the session | `Session(user).active()` | The user comes from your app, never from the model's arguments |
| Argument validation | type hints, `validate={...}` | `str` is a `str`, `Literal` is one of its values; your validators run before anything else |
| Scope limits | `scope={"repo": "acme/gitops-*"}` | The tool can only touch what it declares, whoever asks |
| Action limits | `limit=`, `Toolkit(limits={"destructive": 3})` | A loop or an injection cannot run 200 deletes in one session |
| User authorization | `authorize=permission_check(...)` or any callable | The call runs only if the person asking may do it |
| Untrusted-input check | `untrusted_output=True` on readers | An action whose arguments repeat untrusted content (an email, a web page) is refused |
| Fencing | automatic for untrusted string output; `toolpass.fence()` for any text | Untrusted text reaches the model in a block whose id its author could not know; forged tags are replaced and borrowed frames counted |
| Exfiltration guard | `reads_private=True`, `sends_out=True` | Once a session has read private data and seen untrusted content, nothing is sent out |
| Credential injection | `credential="name"` | The secret is fetched after every check and never enters the model's context; echoes of it in the output are redacted |
| Audit | `Toolkit(audit=...)` | One event per call, refused or run, with who asked and why it was decided |
| Observed actions | `Toolkit(permissions=tp)`, no `effect` | Every request the body makes is mapped to an action, inside `as_user()` checked against the user and refused before it is sent; outside it runs on the agent's authority (`act_as="user"` checks every request but those in `as_agent()`) |
| Recorded entitlements | `Toolkit(record=...)`, then `entitlements=` | A run records what the tool did; the tool is then held to it, with the user's actions checked before the body runs |

## The order of checks

Every call goes through the same steps. A failed step refuses the call before the tool's body runs,
and anything that cannot be evaluated (an authorizer that raises, a credential source that is down)
refuses too.

1. **Session.** No current session means no call.
2. **Arguments.** Types, then validators, then scope rules.
3. **Limits.** One slot is taken from each cap, and given back if the call never reaches its body.
4. **Authorization.** Only an explicit allow lets the call through.
5. **Untrusted input.** Arguments repeating six or more consecutive words of untrusted output
   are refused.
6. **Exfiltration.** A `sends_out` tool in a session that has read private data and seen
   untrusted content is refused (`exfiltration="off"` turns the guard off).
7. **Credential.** Fetched only now.
8. **The body.** Its output is redacted, recorded if untrusted, and fenced.
9. **Audit.** One event, whatever happened.

## The exfiltration guard

An agent can be made to leak data only when one session has all three of what Simon Willison calls
the lethal trifecta: access to private data, exposure to untrusted content, and a way to send data
out. Tools already declare which of these they are, so the toolkit tracks the first two per
session and guards the third:

```python
@toolpass.tool(effect="read", reads_private=True)
def read_customers() -> str: ...

@toolpass.tool(effect="read", untrusted_output=True)
def read_email(id: str) -> str: ...

@toolpass.tool(effect="write", sends_out=True)
def post_slack(channel: str, text: str) -> str: ...
```

After `read_customers` and `read_email` have both run in a session, `post_slack` is refused.
This does not depend on spotting injected words, so a model that paraphrases the injection does not
get past it.

## Hooks in async tools

In async tools, sync hooks (an authorizer, a credentials callable) run in a worker thread, so
blocking I/O in them cannot stall the event loop; pass `async def` hooks to run them on the loop.

## Watch it live

`toolpass.watch()` serves a page that shows every call the toolkit decides, as it happens: who
asked, the arguments, ran or refused and why, each request the body made and the system it reached, with
a map of the tools and those systems. It is read-only: it
changes nothing in the toolkit.

```python
import toolpass

page = toolpass.watch()                    # the default toolkit; toolpass.watch(kit) for another
print(page.url)                            # http://127.0.0.1:PORT/t/<token>/
...
page.close()                               # or use it as a context manager
```

`page.events()` returns what the page shows.

The page binds to the loopback interface only, and every path carries a random token, so another
program or web page on the machine cannot read anything without the printed URL. It has no
accounts: to show it to people elsewhere, put something that authenticates them in front of it.

## Refusals

A refused call raises `ToolRefused` with a `code` (`out_of_scope`, `not_authorized`,
`untrusted_input`, `exfiltration_risk`, ...) and a message written for the model. Three codes
answer "may this user do this?" and must not be confused: `not_authorized` (the user was checked
and lacks access), `authorization_error` (the question could not be answered: an upstream timeout,
an `unknown` answer, an unreachable toolpass; try again shortly) and `not_configured` (nothing was
checked: toolpass has no such connection or action, a server refused the client's key, or the
toolkit has no permissions engine; an operator must configure it). Only `not_authorized` says the user may not. Most frameworks pass
the exception's text back to the model. Where a framework hides it, `Toolkit(on_refuse=str)`
returns the message as the tool's result instead. A check that itself fails unexpectedly (a
scope predicate that crashes, a session source that is down) refuses with `check_error` or the
check's own code.

An exception from the tool's own body passes through unchanged, with two exceptions. If its message
contains the injected credential, or the tool is marked `untrusted_output` (its error text may
carry what it fetched), it is raised as `ToolError`: the secret redacted, the untrusted text
recorded and fenced, and the original exception kept on `.original` for your own logs.

A validator accepts by returning `True` (or any truthy value, such as a `re.Match`) and rejects by
returning anything falsy (`False`, `None`) or raising. Generator tools are not supported: return
the whole result. A returned iterator is turned into a list before redaction; an async iterator
is refused.

## Fencing untrusted text

`toolpass.fence(payload, label=..., source=...)` returns `(block, frames)`: the payload between two
tags whose id is minted after the payload was fetched, a preamble telling the model that everything
inside is data, and a fixed closing line (`toolpass.FENCE_END`) after which trusted text resumes.
Fence tags carrying any other id, and the closing line, are replaced in the payload with
`[forged fence tag]`, and the preamble says how many. Frames borrowed from other trust systems
(`<system-reminder>`, `function_calls` / `function_results`, `"type": "tool_result"`, a `Human:`,
`Assistant:` or `System:` line, ChatML markers) are counted after NFKC, logged and left as they
are; `frames` is that count. The label (often a path the model chose) is clipped to 300 characters,
kept to one line and JSON-quoted. `label` and `source` are keyword-only.

```python
block, frames = toolpass.fence(event.diff, label=event.ref, source="CI event")
prompt = f"Review requested by {requester} for {event.ref}.\n{block}"
```

It works on text no tool produced (a chat thread's earlier messages, a CI event's diff) and on part
of a result. `untrusted_output=True` fences a tool's whole string output with the same function,
unless the body already fenced its untrusted part itself (a whole block, minted during the call).

## Permission checks in tools

`permission_check` asks `Toolpass`, the permission engine in this package, whether the session's
user may perform the action, live, in the system that owns the resource (GitHub, Jira, Kubernetes,
AWS, and 18 more). It works with `Toolpass.from_config(...)` in-process or `Toolpass.remote(...)`
against a toolpass server. Any callable that returns `AuthDecision`, `True` or `False` can be the
authorizer instead. The engine also works on its own:
[permission checks on their own](#permission-checks-on-their-own), below.

## Actions observed at the wire

A declaration is a claim. toolpass can instead watch the requests a tool's body makes: every HTTP
request that leaves the process while a toolpass tool runs (PyGithub, slack_sdk, boto3, raw
`requests`, anything on urllib3, the standard library, httpx or aiohttp) is mapped to the action and
resource the permission engine answers for; inside `as_user()` it is checked against the user and
refused before it is sent. The tool declares nothing:

```python
toolpass.configure(permissions=Toolpass.from_config("toolpass.yaml"))

@toolpass.tool()
def propose_fix(repo: str, path: str, fix: str) -> str:
    repo_ = Github(auth=token).get_repo(repo)
    current = repo_.get_contents(path)                     # the agent reads on its own authority
    with as_user():                                        # checked against the user
        return repo_.create_pull(title="fix", body=fix, head="fix", base="main").html_url
```

When the session's user may not open pull requests in that repository, the `POST` never reaches
GitHub: `ActionRefused` (a `ToolRefused`) is raised inside PyGithub's call, and the call is refused
even if the body catches it. The audit event lists every request as `actions`.
`toolpass.configure(record="toolpass-tools.yaml")` writes the actions a run performed to a file, and
`configure(entitlements="toolpass-tools.yaml")` then checks the recorded user actions before the
body runs and refuses anything beyond the list. `configure(act_as="user")` turns the default
around: every request is checked against the user, except those inside `as_agent()`. [The guide](../docs/guides/intercepted-actions.md)
has the details; `toolpass routes github` prints what toolpass understands of an API.

## Limits

- **The hook sees HTTP from the libraries it patches** (urllib3 and everything on it, the standard
  library, httpx, aiohttp), in the thread or task the body runs in. gRPC, a custom httpx transport
  and a thread an SDK starts (boto3's managed transfers) are not seen. Every integration has a route table (`toolpass routes`);
  a write a table cannot map exactly is refused; such a read goes through unchecked, marked untrusted.
- **The untrusted-input check matches words, not meaning.** A model that paraphrases injected text,
  or an argument shorter than six words, is not caught. The exfiltration guard catches a
  paraphrase only when the session also read private data and the tool sends data out; otherwise
  the call runs, within its scope and limits. Don't rely on this check alone.
- **Fencing helps, it does not guarantee.** Models can still be talked past markers.
- **Only string output is fenced.** Structured output is recorded as untrusted but returned as is.
  A body that calls `fence()` on part of its result is trusted to have fenced every untrusted part.
- **Sessions live in memory.** Counts and flags do not survive a restart or span processes yet.
- **Redaction matches the credential's literal text.** An encoded or split echo of it (base64, a
  hex dump) is not caught, and strings shorter than 8 characters are never redacted.
- **`permission_check` puts argument values into the resource string as they are.** Give such
  arguments a scope rule or validator, so a value carrying `?`, `@` or `#` cannot make the permission
  check look at a different resource from the one the tool acts on.
- **Check and action are not atomic.** Authorization is checked, then the body runs; a permission
  revoked in between is not noticed.
- **Session state reaches worker threads only through context.** Frameworks that run tools in
  threads must copy the context (LangChain and `asyncio.to_thread` do).

## Run the example and the tests

```sh
pip install -e . pytest pytest-timeout hypothesis
python examples/ops_agent.py
python -m pytest -q
```

`examples/ops_agent.py` runs a scripted ops-agent session with no network: an injected email, a
blocked exfiltration attempt, a permission denial, and the same pull request run for an admin.

## Permission checks on their own

Permission checks for AI agents and bots, answered live by the system they act in.

Before your agent acts for a user (delete a Jira issue, push to a repository, scale a deployment),
ask toolpass whether that user may do it. toolpass asks the system that owns the resource, live,
with its own read-only credential, and answers `allow`, `deny` or `unknown`. It only checks; it
never performs the action. Twenty-two systems are supported: Kubernetes, Argo CD, GitHub, GitLab,
Bitbucket, Jira, Confluence, Slack, Datadog, PagerDuty, AWS, Google Workspace, Google Cloud,
Microsoft 365, Databricks, Salesforce, Snowflake, Vault, Azure, Linear, Zendesk and Okta.

```sh
pip install toolpass
```

Python 3.10 or later. The core depends only on PyYAML. If `toolpass` is not found afterwards (pip's
scripts folder is not on PATH, common on Windows), run `python -m toolpass` (`py -m toolpass`). The engine runs in your process, so there is
no service to deploy; the same package also runs it as a server (`toolpass serve`).

### In-process

From a toolpass YAML file ([example](https://github.com/roee-hersh/toolpass/blob/main/examples/toolpass.yaml),
[reference](https://github.com/roee-hersh/toolpass/blob/main/docs/reference/configuration.md)):

```python
from toolpass import Toolpass

tp = Toolpass.from_config("toolpass.yaml")

d = tp.check("dana@example.com", "jira-main", "DELETE_ISSUES", "issue:PAY-123")
d.decision  # "allow", "deny" or "unknown"
d.reason    # "denied: Dana Levi does not hold DELETE_ISSUES on issue PAY-123"
d.allowed   # True only for allow

tp.allowed(...)                # True or False
tp.require(..., fresh=True)    # the decision on allow; raises PermissionDenied otherwise
await tp.acheck(...)           # check and require for async code
await tp.arequire(...)
```

Or with the connections in code, as mappings with exactly the keys of the file. A secret key takes
`toolpass.env("NAME")`, `toolpass.file("/path")`, `toolpass.literal(value)`, or the file's
`"env:NAME"` and `"file:/path"` strings:

```python
import toolpass

tp = toolpass.Toolpass(connections=[
    {"id": "jira-main", "integration": "jira", "url": "https://acme.atlassian.net",
     "username": "toolpass-bot@acme.com", "credential": toolpass.env("JIRA_TOKEN")},
])
```

`check` never raises for a failed lookup: a timeout, a rejected credential or anything else toolpass
cannot evaluate is `unknown`. Treat `unknown` exactly like `deny`.

### Remote

To keep the lookup credentials out of the agent's process, run toolpass as a server (the
`ghcr.io/roee-hersh/toolpass` image, the Helm chart, or `toolpass serve -config toolpass.yaml`) and
ask it:

```python
tp = Toolpass.remote("https://toolpass.internal", api_key)  # or TOOLPASS_URL and TOOLPASS_API_KEY
```

The methods are the same. The URL must be `https://`, or `http://` on localhost; redirects are not
followed; a server that cannot be reached is `unknown`.

### guarded

Puts the check in front of one function, so its body runs only after toolpass said `allow`:

```python
from contextvars import ContextVar
from toolpass import guarded

current_user: ContextVar[str] = ContextVar("current_user")  # set from your login, per request

@tool  # any framework's decorator
@guarded(tp, "jira-main", "DELETE_ISSUES", "issue:{key}", user=current_user, fresh=True)
def delete_issue(key: str) -> str:
    jira.delete_issue(key)  # the agent's own credential, only after allow
    return f"deleted {key}"
```

The user comes from `user=` (a string, a zero-argument callable or a `ContextVar`), never from the
call's arguments, so the model cannot choose who it acts as. Anything but `allow` raises
`PermissionDenied` before the body runs; `deny=` returns a message instead.

### Framework adapters

Each adapter is configured once on the agent with rules, a tool name to `Rule(connection, action,
resource)` or a `(connection, action, resource)` tuple, and checks every call to a tool with a rule
before it runs. A refused call does not run; the model reads `toolpass refused this call: <reason>`
and the run goes on. The user always comes from the application, never from the model. A tool
without a rule runs unchecked, unless `strict=True`.

**Strands Agents**: an intervention handler; the user from `invocation_state`.

```python
from toolpass.strands import ToolpassAuthorization, Rule

guard = ToolpassAuthorization(tp, {"delete_issue": Rule("jira-main", "DELETE_ISSUES", "issue:{key}", fresh=True)})
agent = Agent(tools=tools, interventions=[guard])
agent(prompt, invocation_state={"user_id": user.email})
```

**LangChain and LangGraph**: agent middleware; the user from the runtime context.

```python
from toolpass.langchain import ToolpassMiddleware, Rule

guard = ToolpassMiddleware(tp, {"delete_issue": Rule("jira-main", "DELETE_ISSUES", "issue:{key}")})
agent = create_agent(model, tools=tools, middleware=[guard], context_schema=Context)
agent.invoke({"messages": [...]}, context=Context(user_id=user.email))
# a graph you build yourself: guard.tool_node(tools) is a checked ToolNode
```

**MCP servers** (any SDK): no adapter. `toolpass.tool` on the tool, and the user from the
`X-Toolpass-User` header the agent sends, which `session_from_header` reads on the server's
Streamable HTTP app (this machine only, unless given `key=`); for stdio, a `Session` around `run()`
for the user the host names. A server people sign in to with their own token takes the user from
that token: `toolpass.configure(session=toolpass.mcp.token_session())`.

```python
import toolpass
from toolpass import session_from_header

@mcp.tool()
@toolpass.tool()
def delete_issue(key: str) -> str: ...

app = session_from_header(mcp.streamable_http_app())   # key=... when the agent runs elsewhere
```

**OpenAI Agents SDK**: tool guardrails; the user from the run context's `user_id`.

```python
from toolpass.openai_agents import ToolpassGuardrails, Rule

agent = ToolpassGuardrails(tp, {"delete_issue": Rule("jira-main", "DELETE_ISSUES", "issue:{key}")}).apply(agent)
await Runner.run(agent, prompt, context=RequestContext(user_id=user.email))
```

**Claude Agent SDK**: a `PreToolUse` hook; the user from `user=`. Rules use Claude Code's tool names.

```python
from toolpass.claude_agent_sdk import ToolpassHooks, Rule

guard = ToolpassHooks(tp, {"mcp__ops__delete_issue": Rule("jira-main", "DELETE_ISSUES", "issue:{key}")}, user=current_user)
options = guard.apply(ClaudeAgentOptions(mcp_servers={"ops": server}))
```

**Google ADK**: tool callbacks (or `.plugin()` for an `App`); the user is the session's `user_id`.

```python
from toolpass.google_adk import ToolpassCallbacks, Rule

agent = ToolpassCallbacks(tp, {"delete_issue": Rule("jira-main", "DELETE_ISSUES", "issue:{key}")}).apply(agent)
```

**CrewAI**: process-wide tool-call hooks; the user from the crew's kickoff `inputs`.

```python
from toolpass.crewai import ToolpassHooks, Rule

with ToolpassHooks(tp, {"delete_issue": Rule("jira-main", "DELETE_ISSUES", "issue:{key}")}):
    crew.kickoff(inputs={"user_id": user.email})
```

**Pydantic AI**: a capability (or `ToolpassToolset` around one toolset); the user from `deps.user`.

```python
from toolpass.pydantic_ai import ToolpassAuthorization, Rule

agent = Agent(model, deps_type=Deps, tools=tools,
              capabilities=[ToolpassAuthorization(tp, {"delete_issue": Rule("jira-main", "DELETE_ISSUES", "issue:{key}")})])
agent.run_sync(prompt, deps=Deps(user=user.email))
```

**LlamaIndex**: wrapped tools; the user from `user=`.

```python
from toolpass.llamaindex import ToolpassAuthorization, Rule

guard = ToolpassAuthorization(tp, {"delete_issue": Rule("jira-main", "DELETE_ISSUES", "issue:{key}")}, user=current_user)
agent = FunctionAgent(tools=guard.wrap(tools), llm=llm)
```

Complete, runnable examples:
[toolpass-py/examples](https://github.com/roee-hersh/toolpass/tree/main/toolpass-py/examples). Each
adapter's docstring and the
[agent guide](https://github.com/roee-hersh/toolpass/blob/main/docs/guides/agent-tools.md) cover
groups, `strict`, and what each framework does with a refusal.

### Extras

| Extra | Adds |
|---|---|
| `toolpass[crypto]` | `cryptography`, for the integrations that sign with a private key: GitHub App, Google service accounts, Snowflake key pair, Salesforce JWT, Microsoft 365 certificates |
| `toolpass[strands]` | `strands-agents`, for `toolpass.strands` |
| `toolpass[langchain]` | `langchain` and `langgraph`, for `toolpass.langchain` |
| `toolpass[openai-agents]` | `openai-agents`, for `toolpass.openai_agents` |
| `toolpass[claude-agent-sdk]` | `claude-agent-sdk`, for `toolpass.claude_agent_sdk` |
| `toolpass[google-adk]` | `google-adk`, for `toolpass.google_adk` |
| `toolpass[pydantic-ai]` | `pydantic-ai-slim`, for `toolpass.pydantic_ai` |
| `toolpass[crewai]` | `crewai`, for `toolpass.crewai` |
| `toolpass[llamaindex]` | `llama-index-core`, for `toolpass.llamaindex` |

### The command

The package installs `toolpass`:

```sh
toolpass validate -config toolpass.yaml      # the file, references, certificates; no network
toolpass probe    -config toolpass.yaml      # each connection's credential, live
toolpass check    -config toolpass.yaml -connection jira-main \
  -user dana@example.com -action DELETE_ISSUES -resource issue:PAY-123
toolpass catalog  jira                       # an integration's keys and actions
toolpass serve    -config toolpass.yaml      # POST /check and GET /healthz on :8080
```

### More

- [Documentation](https://github.com/roee-hersh/toolpass/tree/main/docs), including one page per
  integration with the credential to create.
- [Architecture and trust boundaries](https://github.com/roee-hersh/toolpass/blob/main/docs/concepts/architecture.md).
- `toolpass-client` on PyPI, the old Python client, is replaced by this package and gets no new
  releases; installed versions keep working. To move, depend on `toolpass` and change
  `from toolpass_client import Toolpass` / `Toolpass()` to `from toolpass import Toolpass` /
  `Toolpass.remote()` (and `toolpass_client.strands` to `toolpass.strands`).

Apache-2.0.

# Add agent-authorizer to your agent

Your agent acts in other systems with its own credential. This guide adds one check in front of
each tool that changes something, so the tool runs only when the person asking is allowed to do
it themselves.

The examples use the `demo` connection of [`examples/agent-authorizer.yaml`](../../examples/agent-authorizer.yaml),
in which `admin@example.com` may write and `dana@example.com` may not. The
[quickstart](../quickstart.md) shows it working.

## 1. Install

```sh
pip install "agent-authorizer[strands]"   # or [langchain], [openai-agents], [claude-agent-sdk],
                                  # [google-adk], [crewai], [pydantic-ai], [llamaindex]
npm install agent-authorizer-client       # Node, talking to an agent-authorizer server
```

Then make one `AgentAuthorizer` for the process:

```python
from agent_authorizer import AgentAuthorizer

tp = AgentAuthorizer.from_config("agent-authorizer.yaml")     # the engine in this process
tp = AgentAuthorizer.remote(url, api_key)             # or an agent-authorizer server (AGENT_AUTHORIZER_URL, AGENT_AUTHORIZER_API_KEY)
```

In-process, the agent's process holds agent-authorizer's lookup credentials; with a server, it holds only
the API key. [Deploy](deploy.md#pick-a-topology) helps you choose. Everything below works the same
with either.

## 2. Decide which tools to guard

Guard the tools that change state, and only those.

- **Reads stay unguarded.** Give the agent a read-only account for looking at things, and let it
  read freely. A check on every read adds a lookup and nothing else.
- **Each write tool gets one check**, just before it acts.
- **Fewer write tools is better.** If the agent changes production only by opening a pull request,
  there is one tool to guard, and the merge stays with your reviewers.

For example, an ops agent with one read tool and one write tool:

```python
from agent_authorizer.strands import AgentAuthorizerAuthorization, Rule

guard = AgentAuthorizerAuthorization(tp, {
    # service_health has no rule: it reads with a read-only account and runs unchecked
    "open_config_pr": Rule("github-main", "repo.push", "repo:{owner}/{repo}", fresh=True),
})
```

Ask about what the tool really does. `open_config_pr` pushes a branch, so it asks for `repo.push`.
`pr.create` would be too weak: GitHub lets a user with read access open a pull request from a fork.

Use `fresh=True` on tools with lasting effect (delete, merge, scale, push). It makes agent-authorizer ask
the system now instead of reusing a cached answer.

## 3. Set the user from your login, not from the model

The user must come from your own authentication: the SSO session, the Slack user who wrote the
message. Never from a tool argument or the message text, because the model writes those and could
be talked into writing an admin's email.

Every adapter reads the user from something your application passes and the model cannot write:
Strands' `invocation_state`, LangChain's runtime context, the OpenAI Agents run context, the ADK
session, CrewAI's kickoff inputs, Pydantic AI's `deps`, an MCP access token. Where the framework
has no such place (the Claude Agent SDK, LlamaIndex, `guarded`), it takes `user=`: a string, a
zero-argument callable, or a `ContextVar` you set where you handle the request, before the agent
runs:

```python
from contextvars import ContextVar

current_user: ContextVar[str] = ContextVar("current_user")

@app.post("/chat")
async def chat(body: ChatIn, user: User = Depends(authenticated_user)):  # your auth
    current_user.set(user.email)
    agent = Agent(tools=tools, interventions=[guard])          # one agent per request
    result = await agent.invoke_async(body.message, invocation_state={"user_id": user.email})
    return {"reply": str(result)}
```

In a Slack bot, take the user from the event Slack signed:

```python
@app.event("app_mention")
async def on_mention(event, client, say):
    info = await client.users_info(user=event["user"])  # needs the users:read.email scope
    current_user.set(info["user"]["profile"]["email"])
    ...  # run the agent
```

In a shared thread, the user is whoever wrote the message the agent is acting on, not whoever
started the thread.

In Node, use `AsyncLocalStorage` the same way:
`currentUser.run(req.user.email, () => runAgent(...))`.

## 4. Check the write tools

There are two ways, and both fail closed: anything but `allow` (including `unknown`, and an agent-authorizer
server that cannot be reached) stops the call before the tool runs.

**An adapter**, configured once on the agent. It checks every call to a tool that has a rule, and
the tools need no decorator. This is the usual choice; the recipes below show each framework.

```python
guard = AgentAuthorizerAuthorization(tp, {
    "delete_issue": Rule("jira-main", "DELETE_ISSUES", "issue:{key}", fresh=True),
    "close_issue": ("jira-main", "TRANSITION_ISSUES", "issue:{key}"),  # a tuple works too
})
```

- `"jira-main"` is the connection id in `agent-authorizer.yaml`.
- `"DELETE_ISSUES"` is the action. `agent-authorizer catalog jira` lists them.
- `"issue:{key}"` builds the resource from the tool's input.

**`guarded`**, on one function, directly under your framework's tool decorator:

```python
from agent_authorizer import guarded

@tool
@guarded(tp, "jira-main", "DELETE_ISSUES", "issue:{key}", user=current_user, fresh=True)
def delete_issue(key: str) -> str:
    """Delete a Jira issue."""
    jira.delete_issue(key)  # the agent's own credential
    return f"deleted {key}"
```

If the answer is not `allow`, the body never runs and `PermissionDenied` is raised. The tool's
signature does not change, so the model never sees a `user` field.

The adapters share these rules:

- A tool without a rule runs unchecked, so reads stay fast. With `strict=True` it is refused
  instead, and a rule of `None` names a tool that may run unchecked. Use `strict=True` when the
  agent loads tools you do not list yourself, such as MCP tools. A rule that names no tool of the
  agent is logged as a warning: it is usually a typo that leaves the real tool unchecked.
- Each field in a resource must be a plain `str` or `int` input of the tool, and the model's value
  must already have exactly that JSON type, so the resource agent-authorizer checks is the one the tool
  gets (frameworks turn `"07"` into the integer 7). A field the model leaves out takes the tool's
  default.
- A missing user, a resource the input cannot fill, any answer but `allow`, and any error inside
  the check all refuse the call.
- Groups, for systems that grant by group: `groups_key=` (read like the user) or `groups=` (a
  source like `user=`), as the adapter's docstring says.
- Add the adapter after anything that rewrites tool calls (list it last among Strands
  interventions and LangChain middleware, call `guard` after other MCP middleware): a rewrite after
  the check changes what runs after agent-authorizer checked it. A call moved to another tool is refused.
- After a checked tool has run, the adapter logs the
  [write log line](../reference/client.md#the-write-log-line) on the `agent_authorizer` logger.

## 5. Tell the model why

The adapters give the model `agent-authorizer refused this call: <reason>`, naming the user, the action and
agent-authorizer's reason, as the tool's result, and the run goes on. The model can explain it to the
person.

With `guarded`, some frameworks hide exception text from the model. For those, return the refusal
instead of raising it, with `deny=`:

```python
@guarded(..., deny=lambda e: f"refused: {e}")
```

| Framework | With `guarded`, use |
|---|---|
| LangChain, LangGraph | `deny=` (an unexpected exception ends the run) |
| MCP server (`mcp` package) | `deny=` (the server hides exception text) |
| Strands Agents | raise (reported as a tool error with the text) |
| Claude Agent SDK | raise (reported as an `is_error` result with the text) |
| Vercel AI SDK | `deny:` (so the model reads the reason whatever the SDK does with errors) |
| MCP TypeScript SDK | raise (becomes an `isError` result with the text) |

## Framework recipes

Each Python recipe has a complete example in [`agent-authorizer-py/examples`](../../agent-authorizer-py/examples)
that runs the engine in-process on the `demo` connection and drives a real model; each adapter's
docstring has every option.

| Framework | Adapter | Example |
|---|---|---|
| Strands Agents | `agent_authorizer.strands.AgentAuthorizerAuthorization` | [`strands_agent.py`](../../agent-authorizer-py/examples/strands_agent.py) |
| LangChain, LangGraph | `agent_authorizer.langchain.AgentAuthorizerMiddleware` | [`langchain_agent.py`](../../agent-authorizer-py/examples/langchain_agent.py) |
| MCP servers, any host | `agent_authorizer.tool` and `session_from_header`, no adapter | [`mcp_server.py`](../../agent-authorizer-py/examples/mcp_server.py) |
| OpenAI Agents SDK | `agent_authorizer.openai_agents.AgentAuthorizerGuardrails` | [`openai_agents_agent.py`](../../agent-authorizer-py/examples/openai_agents_agent.py) |
| Claude Agent SDK | `agent_authorizer.claude_agent_sdk.AgentAuthorizerHooks` | [`claude_agent_sdk_agent.py`](../../agent-authorizer-py/examples/claude_agent_sdk_agent.py) |
| Google ADK | `agent_authorizer.google_adk.AgentAuthorizerCallbacks` | [`google_adk_agent.py`](../../agent-authorizer-py/examples/google_adk_agent.py) |
| CrewAI | `agent_authorizer.crewai.AgentAuthorizerHooks` | [`crewai_agent.py`](../../agent-authorizer-py/examples/crewai_agent.py) |
| Pydantic AI | `agent_authorizer.pydantic_ai.AgentAuthorizerAuthorization` | [`pydantic_ai_agent.py`](../../agent-authorizer-py/examples/pydantic_ai_agent.py) |
| LlamaIndex | `agent_authorizer.llamaindex.AgentAuthorizerAuthorization` | [`llamaindex_agent.py`](../../agent-authorizer-py/examples/llamaindex_agent.py) |
| Vercel AI SDK (TypeScript) | `guarded` from `agent-authorizer-client` | [`ai_sdk_tool.ts`](../../examples/agent-ts/ai_sdk_tool.ts) |
| MCP TypeScript SDK | `guarded` from `agent-authorizer-client` | [`mcp_server.ts`](../../examples/agent-ts/mcp_server.ts) |

**Strands Agents** (`agent-authorizer[strands]`). An intervention handler; it composes with Strands' other
interventions such as `CedarAuthorization`. The user is `invocation_state["user_id"]` (`user_key=`
names another key):

```python
from agent_authorizer.strands import AgentAuthorizerAuthorization, Rule

guard = AgentAuthorizerAuthorization(tp, {
    "open_config_pr": Rule("github-main", "repo.push", "repo:{owner}/{repo}", fresh=True),
    "delete_issue": ("jira-main", "DELETE_ISSUES", "issue:{key}"),
})
agent = Agent(tools=tools, interventions=[guard])
agent(body.message, invocation_state={"user_id": user.email})  # from your auth, per request
```

**LangChain and LangGraph** (`agent-authorizer[langchain]`). Agent middleware for `create_agent`. The user
is the runtime context's `user_id` (an attribute, or a key for a `TypedDict` context), or else
`user=`. A refusal is a `ToolMessage` with `status="error"`:

```python
from dataclasses import dataclass
from agent_authorizer.langchain import AgentAuthorizerMiddleware, Rule

@dataclass
class Context:
    user_id: str

guard = AgentAuthorizerMiddleware(tp, {"delete_issue": Rule("jira-main", "DELETE_ISSUES", "issue:{key}", fresh=True)})
agent = create_agent(model, tools=tools, middleware=[guard], context_schema=Context)
agent.invoke({"messages": [...]}, context=Context(user_id=user.email))
```

For a LangGraph graph you assemble yourself, `guard.tool_node(tools)` is a `ToolNode` with the
same check.

**MCP servers** (the official `mcp` package or FastMCP; no extra). No adapter: the tools are
plain functions under `agent_authorizer.tool`, and the server only needs the user. The agent names the
user in the `X-Agent-Authorizer-User` header of every request, set once on its MCP client from the login
the agent already did; `session_from_header` wraps the server's Streamable HTTP app and runs each
request under a `Session` for that user (`X-Agent-Authorizer-Groups`, comma-separated, names the user's
groups; `X-Agent-Authorizer-Token` carries their ID token for a system whose groups agent-authorizer reads from
it). The requests of one conversation share one `Session`, so a tool's limits, the
untrusted-input check and the exfiltration guard hold across its calls: the agent names the
conversation in `X-Agent-Authorizer-Session` (its own chat or thread id), or else the MCP session is the
conversation (a sessionless client has none, and then each request is a session of its own). A
request without the user header runs under no session, so an agent-authorizer tool refuses it. The SDK reports an exception
as only `Error executing tool`, so set `on_refuse=str` and the model reads the reason:

```python
import agent_authorizer
from agent_authorizer import session_from_header

agent_authorizer.configure(permissions=tp, on_refuse=str)

@mcp.tool()
@agent_authorizer.tool()
def delete_issue(key: str) -> str:
    with agent_authorizer.as_user():                            # checked against the user in the header
        jira.delete_issue(key)
    return f"deleted {key}"

app = session_from_header(mcp.streamable_http_app())   # FastMCP: mcp.http_app()
uvicorn.run(app, host="127.0.0.1", port=8000)
```

Whoever can reach the server can set the header, so the header is for a server only your agent
reaches, and the agent sets it from the user it authenticated: the Claude Agent SDK with
`mcp_servers={"ops": {"type": "http", "url": url, "headers": {"X-Agent-Authorizer-User": user}}}`, the `mcp`
client through the headers of the `httpx` client it is given. Without `key=` the wrapper takes
requests from the same machine only (loopback or a Unix socket) and refuses others with 403. When
the agent runs elsewhere, or a proxy on the same machine forwards other callers (its requests look
local), pass `key=`, a secret the agent sends in `X-Agent-Authorizer-Key` on every
request; a request without it is refused before anything runs. The user logs in only to your app,
never to the server. Never hand the key to the people using the agent: whoever holds it can name
any user.

A server people connect to directly from their own client (Claude Desktop, Claude Code, an IDE),
each signing in with their own account, takes the user from the access token the SDK verified
instead: `agent_authorizer.configure(session=token_session())` with `from agent_authorizer.mcp import token_session`.
The user is the token's `email` claim (`user_claim="sub"` for its subject; a token whose
`email_verified` is false names no one), `groups_claim=` reads the groups, the token itself goes
with every check (a connection with `oidc_issuer` reads the groups from it when its
`oidc_audience` is the audience of the MCP server's tokens), and the calls made with one token
share one session. The header never speaks for such a caller: a request with both an
`Authorization` header and `X-Agent-Authorizer-User` is refused with 400.

Upgrading from `agent_authorizer.mcp.guard` (0.10 and earlier): put `@agent_authorizer.tool()` (with
`authorize=permission_check(...)` for the rule a tool had) under each `@mcp.tool()`, replace
`guard(...)` with `token_session()` or `session_from_header`, and drop the `[mcp]` extra.

A stdio server serves one user, whom the host names when it starts the server: open one `Session`
around `run()`. The example does both:

```sh
pip install mcp
python /path/to/agent-authorizer/agent-authorizer-py/examples/mcp_server.py -http 8000   # the user from the header
claude mcp add agent-authorizer -e AGENT_USER=dana@example.com \
  -- python /path/to/agent-authorizer/agent-authorizer-py/examples/mcp_server.py        # stdio, the user from AGENT_USER
```

The example runs the engine in-process on the `demo` connection. With real credentials, do not run
the engine inside a server a coding agent launches: an agent with shell access on your machine
could read agent-authorizer's secrets. Run agent-authorizer as a shared service behind TLS and use
`AgentAuthorizer.remote(...)` in the MCP server (see the [deploy guide](deploy.md)). The TypeScript server
runs the same way with `node /path/to/agent-authorizer/examples/agent-ts/mcp_server.ts` (Node 22.18 or
later), with `AGENT_AUTHORIZER_URL`, `AGENT_AUTHORIZER_API_KEY`, `AGENT_USER` and, comma-separated, `AGENT_GROUPS`.

**OpenAI Agents SDK** (`agent-authorizer[openai-agents]`). A tool input guardrail on every function tool;
`apply` returns a clone of the agent. The user is the run context's `user_id` (attribute or key),
or `user=`:

```python
from agent_authorizer.openai_agents import AgentAuthorizerGuardrails, Rule

guard = AgentAuthorizerGuardrails(tp, {
    "delete_issue": Rule("jira-main", "DELETE_ISSUES", "issue:{key}", fresh=True),
    "get_issue": None,  # runs unchecked, even with strict=True
})
agent = guard.apply(Agent(name="ops", tools=[get_issue, delete_issue]))
await Runner.run(agent, prompt, context=RequestContext(user_id=user.email))
```

Only function tools have guardrails: a rule naming a hosted tool raises, and so does `strict=True`
on an agent with MCP servers. Apply the guardrails to each agent a handoff can reach.

**Claude Agent SDK** (`agent-authorizer[claude-agent-sdk]`). A `PreToolUse` hook, which sees every tool
call, built-in or MCP. Rules use Claude Code's tool names: `mcp__<server>__<tool>`, or `Bash`,
`Write`, ... The user comes from `user=`; the SDK runs hooks in the task `query()` or
`ClaudeSDKClient.connect()` started, so set a `ContextVar` before that call:

```python
from agent_authorizer.claude_agent_sdk import AgentAuthorizerHooks, Rule

guard = AgentAuthorizerHooks(tp, {
    "mcp__ops__delete_issue": Rule("jira-main", "DELETE_ISSUES", "issue:{key}", fresh=True),
}, user=current_user)
options = guard.apply(ClaudeAgentOptions(mcp_servers={"ops": server}, allowed_tools=[...]))

current_user.set(user.email)  # before query() or client.connect()
async for message in query(prompt=prompt, options=options): ...
```

An allowed call gets no decision from the hook, so Claude Code's own permission rules still apply
after agent-authorizer. `guarded` also works on an SDK MCP tool's handler, whose one parameter is a dict of
arguments.

**Google ADK** (`agent-authorizer[google-adk]`). A `before_tool_callback`, added last among the agent's own
and to its LLM sub-agents. The user is the session's `user_id`, which you pass to the runner, or
`user=`. A refusal answers `{"error": "agent-authorizer refused this call: ..."}`:

```python
from agent_authorizer.google_adk import AgentAuthorizerCallbacks, Rule

guard = AgentAuthorizerCallbacks(tp, {"delete_issue": Rule("jira-main", "DELETE_ISSUES", "issue:{key}", fresh=True)})
agent = guard.apply(LlmAgent(name="ops", model=..., tools=[get_issue, delete_issue]))
runner = InMemoryRunner(agent=agent)
session = await runner.session_service.create_session(app_name=runner.app_name, user_id=user.email)
```

`guard.plugin()` is the same check as an `App` plugin, for every agent at once; plugins run
before an agent's own callbacks, so do not combine it with callbacks that edit tool arguments.

**CrewAI** (`agent-authorizer[crewai]`). A pair of `before_tool_call` and `after_tool_call` hooks. CrewAI
hooks are process-wide: registered, the rules apply to every crew until `unregister()`, or for the
`with` block. The user is the crew's kickoff input `user_id` (`user_input=`), or `user=` (use that
for `Agent.kickoff` without a crew):

```python
from agent_authorizer.crewai import AgentAuthorizerHooks, Rule

with AgentAuthorizerHooks(tp, {"delete_issue": Rule("jira-main", "DELETE_ISSUES", "issue:{key}", fresh=True)}):
    crew.kickoff(inputs={"user_id": user.email, "topic": topic})
```

Rule names match the way CrewAI names tools to the model (`"Read Thing"` and `read_thing` are the
same tool).

**Pydantic AI** (`agent-authorizer[pydantic-ai]`). A capability that wraps every tool the agent has in a
`AgentAuthorizerToolset`. The user is `deps.user` (attribute or key), and the groups `deps.groups` when
present, or `user=` and `groups=`. A refusal is a failed tool result (`ToolFailed`):

```python
from dataclasses import dataclass
from agent_authorizer.pydantic_ai import AgentAuthorizerAuthorization, Rule

@dataclass
class Deps:
    user: str

guard = AgentAuthorizerAuthorization(tp, {"delete_issue": Rule("jira-main", "DELETE_ISSUES", "issue:{key}", fresh=True)})
agent = Agent(model, deps_type=Deps, tools=[delete_issue, read_issue], capabilities=[guard])
agent.run_sync(prompt, deps=Deps(user=user.email))
```

To check only some tools, wrap their toolset instead:
`Agent(model, toolsets=[AgentAuthorizerToolset(FunctionToolset([delete_issue]), tp, rules)])`.

**LlamaIndex** (`agent-authorizer[llamaindex]`). LlamaIndex agents have no hook that can stop a tool call,
so `wrap` puts the check in each tool that has a rule. The user comes from `user=`; the workflow
runs in a copy of the caller's context, so set a `ContextVar` before `agent.run`. A refusal is a
`ToolOutput` with `is_error=True`:

```python
from agent_authorizer.llamaindex import AgentAuthorizerAuthorization, Rule

guard = AgentAuthorizerAuthorization(tp, {"delete_issue": Rule("jira-main", "DELETE_ISSUES", "issue:{key}", fresh=True)},
                                 user=current_user)
agent = FunctionAgent(tools=guard.wrap([delete_issue, read_issue]), llm=llm)

current_user.set(user.email)  # before agent.run
await agent.run(prompt)
```

**Vercel AI SDK** (TypeScript, `agent-authorizer-client`, against an agent-authorizer server):

```ts
const writeThing = tool({
  description: "Write content to a thing.",
  inputSchema: z.object({ thing_id: z.string(), content: z.string() }),
  execute: guarded(tp, "demo", "thing.write", "thing:{thing_id}", { user, deny: (e) => `refused: ${e.message}` })(
    async ({ thing_id, content }) => { ... },
  ),
});
```

## Run the examples

From a clone of the repository, install agent-authorizer with the framework's extra and the model provider
the example names in its docstring, then run it as dana (refused) and as the admin (allowed):

```sh
pip install -e "./agent-authorizer-py[strands]" "strands-agents[anthropic]"
export ANTHROPIC_API_KEY=...
python agent-authorizer-py/examples/strands_agent.py dana@example.com
python agent-authorizer-py/examples/strands_agent.py admin@example.com
```

The Python examples need no agent-authorizer server. The TypeScript examples talk to one:

```sh
export AGENT_AUTHORIZER_API_KEY=change-me
agent-authorizer serve -config examples/agent-authorizer.yaml       # or the Docker command from the quickstart

(cd examples/agent-ts && npm ci)
AGENT_AUTHORIZER_URL=http://localhost:8080 node examples/agent-ts/ai_sdk_tool.ts dana@example.com
```

The adapters' tests drive each framework's real agent loop, with a scripted model, against a real
in-process engine: a refused call does not run and the model reads why, an allowed one runs, and
the model cannot choose the user. With `ANTHROPIC_API_KEY` set, a live test per framework runs the same scenario with
Claude. Each file skips when its framework is not installed:

```sh
(cd agent-authorizer-py && python -m pytest tests/frameworks -v)
(cd examples/agent-ts && npm test)
```

## Checklist

- [ ] Only write tools are checked, one check each, with `fresh=True` where the effect lasts.
- [ ] The user comes from your authentication, set per request, never from the model.
- [ ] Anything other than `allow` stops the tool, including an agent-authorizer server that cannot be reached.
- [ ] The model gets the refusal text, so it can tell the person why.
- [ ] The lookup credentials are where you decided they should be: in the agent's process, or only
      in the agent-authorizer server, with the API key held only by the tool layer.

Every `AgentAuthorizer` method, the `guarded` parameters and the write log line are in the
[Python API reference](../reference/client.md).

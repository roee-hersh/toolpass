# Calling agent-authorizer from a TypeScript agent

Runnable examples for [the agent tools guide](../../docs/guides/agent-tools.md), which explains
the pattern: check with agent-authorizer before acting, treat `unknown` as deny, and
never let the model choose the user. The Python examples are in
[`agent-authorizer-py/examples`](../../agent-authorizer-py/examples). The client these use is
the [`agent-authorizer-client`](../../agent-authorizer-ts) package
(`npm install agent-authorizer-client`), which talks to an agent-authorizer server;
here they import its source from `agent-authorizer-ts` directly.

| File | What it is |
|---|---|
| `ai_sdk_tool.ts` | The two tools as Vercel AI SDK `tool()` definitions, for `generateText`, `streamText` and anything built on them. |
| `mcp_server.ts` | A standalone MCP server over stdio for any MCP host. |
| `fake_agent_authorizer.ts` | The fake agent-authorizer the tests run against. |
| `*.test.ts` | Tests, through each framework's own invocation path. |

Every example has `check_permission`, so the model can ask first, and a
guarded `write_thing` action against the `demo` connection of
[`examples/agent-authorizer.yaml`](../agent-authorizer.yaml), where `admin@example.com` may
write and `dana@example.com` may not.

Needs Node 22.18 or later (it runs `.ts` files directly) and npm.

```sh
# agent-authorizer with the demo connection (pip install agent-authorizer, or the Docker command from the quickstart)
export AGENT_AUTHORIZER_API_KEY=change-me
agent-authorizer serve -config examples/agent-authorizer.yaml

# the examples' dependencies and tests
cd examples/agent-ts
npm ci
npm test

# a script against the live server, no LLM needed
export AGENT_AUTHORIZER_URL=http://localhost:8080
node ai_sdk_tool.ts dana@example.com
node ai_sdk_tool.ts admin@example.com
```

`mcp_server.ts` is launched by the MCP host; see the guide for the
`claude mcp add` line.

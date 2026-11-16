# Calling agent-authorizer from a Python agent

The Python examples moved to [`agent-authorizer-py/examples`](../../agent-authorizer-py/examples). There is one per
framework adapter (Strands Agents, LangChain and LangGraph, MCP, the OpenAI Agents SDK, the Claude
Agent SDK, Google ADK, CrewAI, Pydantic AI and LlamaIndex), each configured once on the agent,
running the agent-authorizer engine in-process on the `demo` connection, so none needs an agent-authorizer server.
[The agent tools guide](../../docs/guides/agent-tools.md) explains the pattern and how to run them.

The examples that used to be here called an agent-authorizer server through `toolpass_client` (the old
Python client, now replaced by `agent-authorizer`: `AgentAuthorizer.remote()` is the same client) and wrapped
each tool with `guarded`, which is `agent_authorizer.guarded`. The adapters' tests in
[`agent-authorizer-py/tests/frameworks`](../../agent-authorizer-py/tests/frameworks) replace these examples' tests.

The TypeScript examples are in [`../agent-ts`](../agent-ts).

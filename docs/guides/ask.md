# Ask who may do what, from your assistant

The toolpass server answers permission questions to an AI assistant too. Connect Claude Code or
any other MCP client to it, and a person can ask in their own words:

> Can Ilya create deployments in the payments namespace on prod-eu?

The assistant finds the connection and the action, asks toolpass, and toolpass asks the cluster,
now, exactly as [`POST /check`](../reference/api.md#post-check) does:

> No. Kubernetes denies `deployment.create` in `namespace:payments` for `ilya@example.com`:
> no RBAC rule grants it.

It is for looking things up: who has access before you grant more, why someone's request was
refused, whether an offboarded account can still do anything.

## What it is not

It does not stop anything. A model cannot be relied on to ask before it acts, so a tool that acts
for a person is held to that person's permissions by [`toolpass.tool`](agent-tools.md), inside the
tool, whether or not anyone asked first. The tools here only read, and nothing an assistant does
with them changes what a secured tool allows.

## Connect

The endpoint is `/mcp` on the server you already run (`toolpass serve`), with nothing to switch on.
It takes the server's `api_key` as a bearer token, like every other route.

Claude Code:

```sh
claude mcp add --transport http toolpass https://toolpass.internal/mcp \
  --header "Authorization: Bearer $TOOLPASS_API_KEY"
```

Or in a project's `.mcp.json`:

```json
{
  "mcpServers": {
    "toolpass": {
      "type": "http",
      "url": "https://toolpass.internal/mcp",
      "headers": { "Authorization": "Bearer ${TOOLPASS_API_KEY}" }
    }
  }
}
```

Try it against the [quickstart](../quickstart.md#3-run-it-as-a-server) server with
`http://localhost:8080/mcp` and `change-me`, and ask "may dana@example.com write thing 1 in demo?".

## The tools

| Tool | Takes | Answers |
|---|---|---|
| `list_connections` | nothing | each connection's id and the kind of system it is (`k8s-prod-eu`, `kubernetes`) |
| `list_actions` | `connection` | the actions that can be asked about there, each with a one-line description, as `toolpass catalog` prints them |
| `check_permission` | `user` (an email), `connection`, `action`, `resource`; optionally `groups` and `fresh` | `decision` (`allow`, `deny` or `unknown`), `reason`, its `code`, and `identity`, the account the email resolved to |

`check_permission` takes the fields of `POST /check` and gives its answer, with the same decision
codes, caches and decision log line. Three things differ:

- **`unknown` is reported as unknown.** A caller about to act treats `unknown` as deny. A person
  asking a question should hear that the cluster was unreachable or that two accounts share the
  email, not "no"; the server's instructions to the assistant say so.
- **A question that could not be asked is an error.** `invalid_request`, `unknown_connection` and
  `unknown_action` come back marked as an error, with the reason, so the assistant fixes the
  question (the reason names the resource shapes the system takes) or asks the person.
- **There is no `token`.** A person's ID token should not pass through a model. For a connection
  that reads groups from the token ([Groups from the user's token](groups.md)), give `groups` when
  you know them, or ask with [`toolpass check`](../reference/cli.md).

The user is an email address. The assistant is told to ask for it rather than guess one from a
name, and to ask which namespace, repository or project is meant when the question does not say.

## Who can ask

Whoever holds the API key can ask about anyone, in every connected system. That is already what
the key means (`POST /check` answers the same questions), but an assistant makes it easy, so give
the key to the people who should be able to look up other people's access, and to nobody else:

- An assistant holding the key answers anyone who can talk to it. In a shared channel or a
  multi-user agent, that is everyone there.
- The answers go to the model, and so to the provider behind it: emails, account names and what
  each may do.
- `reason` and `identity` carry text from the system that was asked (an account's display name,
  for example). The assistant is told to treat them as data; do not connect this endpoint to an
  agent that also holds tools able to act on what it reads without the
  [toolkit's guards](../../toolpass-py/README.md#the-exfiltration-guard).
- Every question is a line in the [decision log](operating.md#decision-log), with the user asked
  about and the caller's address, and each tool call is an `mcp tool` line in the server's log
  (without the user).

There is one key, so the server cannot tell two askers apart. To give some people this and not
`POST /check`, put the server behind a proxy that authenticates them and routes only `/mcp`.

## How it is served

`/mcp` speaks MCP's Streamable HTTP transport with JSON responses: one JSON-RPC request per
`POST`, one JSON object back, no stream and no session, so any replica answers any request and
nothing is kept between them. The details are in the [HTTP API reference](../reference/api.md#post-mcp).

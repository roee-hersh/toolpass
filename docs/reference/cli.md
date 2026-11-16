# Command-line reference

One command, `agent-authorizer`, with nine subcommands plus `agent-authorizer version`. `pip install agent-authorizer`
installs it, and it is the entry point of the Docker image. Every subcommand that reads a config
takes `-config FILE`, defaulting to `/etc/agent-authorizer/agent-authorizer.yaml`. Flags take one dash (`-config
FILE`); `--config=FILE` works too.

| Command | What it does |
|---|---|
| `agent-authorizer serve -config FILE` | Run the service |
| `agent-authorizer validate -config FILE` | Check the file, credential references and certificates. No network |
| `agent-authorizer probe -config FILE [-connection ID]` | Call each system with its credential and report |
| `agent-authorizer check -config FILE -connection ID -user EMAIL -action NAME -resource RES [-group G]... [-json]` | Answer one question from the command line |
| `agent-authorizer check -server URL [-api-key REF] [-ca-file PEM] [-timeout D] [-fresh] ...` | Ask a running agent-authorizer the same question; `-fresh` skips its caches |
| `agent-authorizer catalog [INTEGRATION]` | List integrations, config keys and actions |
| `agent-authorizer routes [INTEGRATION]` | List how requests to an integration's API map to its actions (the secure-tools toolkit's [observed actions](../guides/intercepted-actions.md)) |
| `agent-authorizer gateway off` | Put back the MCP server entries agent-authorizer 0.8 and 0.9 rewrote with `gateway on` |

At startup `serve` reads the API key and exits with an error when it cannot (a server that could
not would report healthy and refuse every check), then probes every connection and logs warnings.
A broken connection never stops the service from starting.

`check` runs the same code path as `POST /check` in-process, so it needs the config file and the
connection's credential but no running server and no API key. Caches and the decision log are off. It prints the decision and reason
(`-json` prints the HTTP response body) and exits 0 for `allow`, 1 for `deny`, 3 for `unknown` and
2 when no decision was reached (bad flags, a config that does not load, an interrupted run), so
`if agent-authorizer check ...` treats `unknown` as deny. Only the connection asked about is built, so a
sibling whose credential is missing on this machine does not get in the way.

```sh
$ agent-authorizer check -config agent-authorizer.yaml -connection jira-main \
    -user dana@example.com -action DELETE_ISSUES -resource issue:PAY-123
deny
  denied: Dana Levi does not hold DELETE_ISSUES on issue PAY-123
```

With `-server URL` the same question goes to a running agent-authorizer over `POST /check`, so it can be
asked from a machine that holds the API key but none of the upstream credentials. `-api-key` is an
`env:NAME` or `file:/path` reference (default `env:AGENT_AUTHORIZER_API_KEY`); a key value on the command
line is rejected. The URL must be `https://` unless it is `localhost` or a loopback address.
Output and exit codes are the same; a reply that is not a decision (wrong host, proxy error page)
or none within `-timeout` (default `1m`) exits 2.

```sh
agent-authorizer check -server https://agent-authorizer.internal -connection jira-main \
    -user dana@example.com -action DELETE_ISSUES -resource issue:PAY-123
```

`serve` also takes `-listen ADDR`, which overrides `listen` in the file, and
`-log-level debug|info|warn|error` (default `info`).

`serve` also collects the runs agents report (`POST /runs`), serves [the dashboard](../guides/dashboard.md)
at `/dashboard/`, and answers permission questions to MCP clients at [`/mcp`](../guides/ask.md). `gateway off` exits 0 and prints one line per entry put back. An entry an earlier
version rewrote starts `agent-authorizer mcp ... -- COMMAND`, which still runs `COMMAND`, unrecorded, with
its exit code.

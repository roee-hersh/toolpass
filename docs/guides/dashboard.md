# The dashboard

The dashboard shows every run of your toolpass [toolpass tools](agent-tools.md), from every agent
and MCP server that reports to one toolpass server: who the tool ran for, each request it made to
GitHub, Jira, Slack, Linear, Kubernetes and the other connected systems, and whether the system let
that person make it. A refused request is never sent, and the dashboard says which system refused
it and why.

It lives on the toolpass server (`toolpass serve`), at `/dashboard/`. Nothing is written to disk
anywhere: agents report their runs to the server over HTTP, the server keeps the latest ones in
memory for the page, and long-term retention is an [export](#export-to-opentelemetry) into the
tracing you already run.

## Turn it on

A toolkit whose permissions come from a toolpass server already knows where to report:

```python
tools = Toolkit(permissions=Toolpass.remote("https://toolpass.internal", api_key))
```

Every run of its tools goes to that server, with nothing else configured. Open
`https://toolpass.internal/dashboard/`; the page asks for the server's API key once (it stays in
the browser tab) and shows the runs live.

A toolkit with an in-process engine reports when the process has `TOOLPASS_URL` and
`TOOLPASS_API_KEY` set, the same variables `Toolpass.remote()` reads by default. For an MCP server
that Claude Desktop or Claude Code starts, put them in the server's `env` in the client's config:

```json
{
  "mcpServers": {
    "shop": {
      "command": "python",
      "args": ["/path/to/shop_server.py"],
      "env": {"TOOLPASS_URL": "https://toolpass.internal", "TOOLPASS_API_KEY": "...", "TOOLPASS_SERVER_NAME": "shop"}
    }
  }
}
```

| Variable | What it does |
|---|---|
| `TOOLPASS_URL`, `TOOLPASS_API_KEY` | the toolpass server a toolkit with an in-process engine reports to (a remote engine uses its own) |
| `TOOLPASS_SERVER_NAME` | this process's name on the page; by default its script's name |
| `TOOLPASS_DASHBOARD` | `off` reports nothing; a URL reports to that server instead, with `TOOLPASS_API_KEY` |

Reporting never slows a tool: one background thread sends the runs in batches, a server that is
down or slow costs nothing but the runs it did not get (said once on stderr), and the queue is
bounded. What is sent is the audit event: the tool, the user, the outcome and why, the arguments
(long strings cut to 200 characters) and each request the body made as toolpass understood it.

## The page

- **The numbers** at the top: tool runs and by how many people, how many went through, how many
  were refused and at which systems, and the systems reached. Click one to filter the list.
- **Where the calls went**: each server or agent, its tools, and the systems each tool reached,
  joined by lines that grow with the traffic and turn red where a request was refused. Hover a box
  to light up what it connects to; click it to narrow the list.
- **The runs**, newest first, each in one line: who ran which tool on which server, where it went
  or where it was stopped, one badge per request it made, the outcome, when and how long. Click a
  run to open it: each request in words (`Open a pull request on acme/shop`), the raw request under
  it, allowed or refused with the reason, then the arguments.
- **Filters**: by person, by outcome, and a search box that matches tools, systems and arguments.
  The page opens on the latest 500 runs; the server keeps the latest 10,000.

Opened, Dana's `open_pr` reads:

```text
GH  Read acme/shop                as the agent   GET  /api/v3/repos/acme/shop                 allowed
GH  Open a pull request on acme/shop             POST /api/v3/repos/acme/shop/pulls           allowed
J   Comment on SHOP-12                           POST /rest/api/3/issue/SHOP-12/comment       allowed
J   Read SHOP-12                                 GET  /rest/api/3/issue/SHOP-12/transitions   allowed
J   Move SHOP-12                                 POST /rest/api/3/issue/SHOP-12/transitions   allowed
S   Post in #C0SHOPDEV                           POST /api/chat.postMessage                   allowed
```

and Sam's, who may read the repository but not open pull requests on it:

```text
GH  Read acme/shop                as the agent   GET  /api/v3/repos/acme/shop                 allowed
GH  Open a pull request on acme/shop             POST /api/v3/repos/acme/shop/pulls           refused
    forking disabled on acme/shop; pull request needs push access, and sam-cohen has only read
```

Each line is the action toolpass named from the request (the integration's route table:
`github:pr.create on repo:acme/shop`), and the decision the system itself gave for the session's
user. Every integration has a route table: `toolpass routes` lists them, `toolpass routes
<integration>` shows one, and each integration's page says what its table checks and what it
refuses. A request a table cannot map exactly is refused when it writes; a read goes through,
recorded without an action.

## Who sees it

The page itself (HTML, script, stylesheet) is public: it holds no data. The runs
(`GET /dashboard/events`) are served to holders of the server's API key, the same key agents use,
and the page asks for it. Runs carry tool arguments, so treat that key as you treat the server:
put the server behind your ingress's own authentication when more people than its operators should
open the page, and give the page a separate hostname or path rule there if you want it reachable
to people who may not call `/check`. A key pasted into the page's address as `#key=...` is taken by
the page and never sent to any server (the demo opens the page that way).

## One process, no server

`toolpass.watch(toolkit)` serves the same page from inside one process, on the loopback interface
behind a random token, with the runs of that toolkit alone and nothing written anywhere:

```python
page = toolpass.watch(tools, open_browser=True)   # page.url; page.close()
```

For one agent on your machine that is enough; the server collects across agents and machines.

## Export to OpenTelemetry

The dashboard shows the latest runs. For retention, search and alerts, export each run as a span
into the tracing your team already has (Grafana Tempo, Datadog, Honeycomb, Jaeger):

```sh
pip install "toolpass[otel]"
```

```python
import toolpass
from toolpass.otel import audit_sink

toolpass.configure(audit=audit_sink())               # the default toolkit
tools = Toolkit(audit=audit_sink(then=log_audit))    # or one toolkit, keeping the JSON log line too
```

One span per run, `toolpass.tool <name>`, with `toolpass.user`, `toolpass.outcome`,
`toolpass.code` and `toolpass.reason` as attributes, timed from the run's own start and duration;
each request the body made is a span event, `toolpass.request`, with the system, the action, the
resource and whether it was allowed. A refused or failed run sets the span's status to ERROR.
Arguments are not exported. The sink uses the global tracer provider your application configured
(`opentelemetry-sdk` and an exporter); pass `tracer=` for another.

## See it with five systems

`examples/demo/run.py` in `toolpass-py` runs the whole picture on your machine, with no accounts:

```sh
cd toolpass-py && pip install -e .
python examples/demo/run.py -open
```

It starts a toolpass server with five connections, then two people, Dana (a developer) and Sam
(support, read-only), call the tools of a `shop` MCP server built with toolpass: `open_pr` (GitHub,
Jira, Slack), `ship` (Kubernetes, Slack), `report_bug` (Linear, Jira, Slack) and `status` (reads).
The shop server asks the toolpass server about each request and reports each run to it. GitHub,
Jira, Slack, Linear and Kubernetes are stand-ins on `127.0.0.1` answering from the grants in
`examples/demo/world.py`; the permission checks are the real integrations'. Dana ships to staging
and is refused prod by Kubernetes RBAC; Sam's writes stop at the first system that says no.
Nothing is written outside a temporary folder. To use the shop server from Claude Code instead,
run the demo once, then add the server with the address and key it prints
(`claude mcp add shop -e DEMO_USER=sam@acme.dev -e TOOLPASS_URL=... -e TOOLPASS_API_KEY=... -- python examples/demo/shop_server.py`)
and ask Claude to open a pull request.

## Upgrading from 0.8, 0.9 and 0.10

0.8 and 0.9 could wrap every MCP server Claude Desktop and Claude Code use (`toolpass gateway on`)
and record their calls; 0.10 recorded the runs of toolpass tools to a SQLite file on the agent's
machine (`TOOLPASS_DASHBOARD=1`, `toolpass dashboard`). Both are gone: toolpass records its own
tools, and the record is on the toolpass server, where every agent can reach it. An entry an
earlier version rewrote still starts its server; `toolpass gateway off` puts every such entry back
as it was. A `dashboard.db` or `gateway.db` left in the per-user data folder is read by nothing and
can be deleted.

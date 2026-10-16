# The dashboard

The dashboard shows every run of your toolpass [secured tools](agent-tools.md), from every MCP
server and agent you run them in: who the tool ran for, each request it made to GitHub, Jira,
Slack, Linear, Kubernetes and the other connected systems, and whether the system let that person
make it. A refused request is never sent, and the dashboard says which system refused it and why.

## Turn it on

Start the process that runs your tools with `TOOLPASS_DASHBOARD=1`, then open the page:

```sh
pip install toolpass
TOOLPASS_DASHBOARD=1 python my_server.py
toolpass dashboard -open
```

For an MCP server that Claude Desktop, Claude Code or another client starts, put the variable in
the server's `env` in the client's config:

```json
{
  "mcpServers": {
    "shop": {
      "command": "python",
      "args": ["/path/to/shop_server.py"],
      "env": {"TOOLPASS_DASHBOARD": "1", "TOOLPASS_SERVER_NAME": "shop"}
    }
  }
}
```

| Variable | What it does |
|---|---|
| `TOOLPASS_DASHBOARD` | `1` writes each run to the record `toolpass dashboard` reads; a path writes to that file instead; unset or `0` writes nothing |
| `TOOLPASS_SERVER_NAME` | the process's name on the page; by default its script's name |
| `TOOLPASS_DATA_DIR` | where the record is kept (see below) |

Every tool of every `Toolkit` in the process is recorded, whatever the framework: an MCP server, a
LangChain or Claude Agent SDK agent, or plain Python. Nothing else needs configuring, and a record
that cannot be written never changes a tool's outcome.

## The page

```sh
toolpass dashboard -open
```

It prints an address on `127.0.0.1` with a random token in the path, and serves until Ctrl-C.

- **The numbers** at the top: tool runs and by how many people, how many went through, how many
  were refused and at which systems, and the systems reached. Click one to filter the list.
- **Where the calls went**: each server, its tools, and the systems each tool reached, joined by
  lines that grow with the traffic and turn red where a request was refused. Hover a box to light
  up what it connects to; click it to narrow the list.
- **The runs**, newest first, each in one line: who ran which tool on which server, where it went
  or where it was stopped, one badge per request it made, the outcome, when and how long. Click a
  run to open it: each request in words (`Open a pull request on acme/shop`), the raw request under
  it, allowed or refused with the reason, then the arguments.
- **Filters**: by person, by outcome, and a search box that matches tools, systems and arguments.
  The page opens on the latest 500 runs.

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

`-listen 127.0.0.1:8899` picks the port. The page binds to the loopback interface only and is
read-only. A single process can also serve a live page of its own runs, without a record:
`toolpass.watch(toolkit)` returns it, and its `url`.

## See it with five systems

`examples/demo/run.py` in `toolpass-py` runs the whole picture on your machine, with no accounts:

```sh
cd toolpass-py && pip install -e .
python examples/demo/run.py -open
```

Two people, Dana (a developer) and Sam (support, read-only), call the tools of a `shop` MCP server
built with toolpass: `open_pr` (GitHub, Jira, Slack), `ship` (Kubernetes, Slack), `report_bug`
(Linear, Jira, Slack) and `status` (reads). GitHub, Jira, Slack, Linear and Kubernetes are
stand-ins on `127.0.0.1` answering from the grants in `examples/demo/world.py`; the permission
checks are the real integrations'. Dana ships to staging and is refused prod by Kubernetes RBAC;
Sam's writes stop at the first system that says no. The record is kept in a temporary folder, not
yours. To use the server from Claude Code instead, add it with the variable set
(`claude mcp add shop -e DEMO_USER=sam@acme.dev -e TOOLPASS_DASHBOARD=1 -- python examples/demo/shop_server.py`)
and ask Claude to open a pull request.

## Where the record is

One SQLite file, `dashboard.db`, in `~/Library/Application Support/toolpass/` (macOS),
`%LOCALAPPDATA%\toolpass\` (Windows) or `~/.local/share/toolpass/` (Linux); `TOOLPASS_DATA_DIR`
moves it. The folder and the file are readable only by you. Arguments are stored as the audit
event carries them (long strings cut to 200 characters). Runs older than 90 days are deleted when
the dashboard starts. Nothing is sent anywhere.

## Upgrading from 0.8 or 0.9

Those versions could wrap every MCP server Claude Desktop and Claude Code use (`toolpass gateway
on`) and record their calls. That is gone: toolpass records its own tools, where it can see each
request, and leaves other servers alone. An entry an earlier version rewrote still starts its
server, unrecorded; `toolpass gateway off` puts every such entry back as it was.

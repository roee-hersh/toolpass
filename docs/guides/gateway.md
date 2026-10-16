# The MCP gateway

The gateway records every tool call Claude Desktop and Claude Code make to their MCP servers, and
shows them on one page. It changes nothing about the calls: the client still asks "Allow this
tool?", and every message goes through byte for byte.

## Turn it on

```sh
pip install toolpass
toolpass gateway on
```

`gateway on` finds the clients' config files, rewrites each stdio server entry to start through
toolpass, and prints what it found:

```text
claude-desktop  github                   recording                        ~/Library/Application Support/Claude/claude_desktop_config.json
claude-desktop  linear                   found (HTTP, not recorded yet)   ~/Library/Application Support/Claude/claude_desktop_config.json
claude-code     sentry                   recording                        ~/.claude.json
```

Restart the clients so they start the servers again. `toolpass gateway status` shows the same list
without changing anything; `toolpass gateway off` puts every entry back.

| Client | File |
|---|---|
| Claude Desktop | `claude_desktop_config.json` in `~/Library/Application Support/Claude/` (macOS), `%APPDATA%\Claude\` (Windows), `~/.config/Claude/` (Linux) |
| Claude Code | `~/.claude.json`: the user's servers and each project's |

A wrapped entry starts `toolpass mcp` with the original command after `--`, from the installation
that wrapped it (`python -P -m toolpass.cli`, or the `toolpass` script on Python 3.10, so nothing is
imported from the folder the client runs it in), and keeps the original under a `"toolpass"` key in the same entry;
the server's `env` is untouched. Because the entry names that installation, removing the environment
toolpass was installed in stops the wrapped servers from starting; `toolpass gateway status` then
reports them as `BROKEN`, and `toolpass gateway off` run from any install puts them back. The first change to a file leaves a copy of the original beside it,
named `<file>.toolpass-backup`. A file that is not valid JSON is left alone.

## The dashboard

```sh
toolpass dashboard -open
```

It prints an address on `127.0.0.1` with a random token in the path, and serves until Ctrl-C.

- **The numbers** at the top: tool calls and by how many people, how many went through, how many
  were refused and at which systems, and the systems reached. Click one to filter the list.
- **Where the calls went**: each MCP server, its tools, and the systems each tool reached (for
  [secured tools](#inside-a-call)), joined by lines that grow with the traffic and turn red where a
  request was refused. Hover a box to light up what it connects to; click it to narrow the list.
- **The calls**, newest first, each in one line: who ran which tool on which server, where it went
  or where it was stopped, one badge per request it made, the outcome, when and how long. Click a
  call to open it: each request in words (`Open a pull request on acme/shop`), the raw request
  under it, allowed or refused with the reason, then the arguments and the result. A call the
  server never answered, because the client or the server stopped first, is marked `no answer`.
- **Filters**: by person, by outcome, and a search box that matches tools, systems and arguments.
  The page opens on the latest 500 calls.
- **Servers**: what the gateway found, whether it is being recorded, how many calls it had, and its
  tools, marked read-only or destructive as the server declares them.

Each time it starts, the dashboard wraps servers added to the clients since the last run, forgets
servers removed from them, and deletes calls older than 90 days; `-no-wrap` leaves the files alone. `-listen 127.0.0.1:8899` picks the port. The page binds to the loopback
interface only and is read-only.

## Inside a call

Most gateways stop at the tool: they show that `open_pr` ran, not what it did. When the server's
tools are toolpass [secured tools](agent-tools.md), the dashboard also shows each request the tool
made to GitHub, Jira, Slack and the other connected systems, under the call that made it. Opened,
Dana's `open_pr` reads:

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
user. A refused request is never sent, and the tool stops there. The gateway passes the server two
environment variables, `TOOLPASS_GATEWAY_SESSION` and `TOOLPASS_GATEWAY_DB`, and the toolkit
writes each tool run to the same record; nothing needs configuring. A run is placed under
the call of its session that was running when it ran; when several were (parallel tool use), the
one of the same tool, paired in the order they were sent. A server whose tools are not secured
tools shows the MCP call only.

GitHub, Slack, Jira, Confluence, Zendesk, Linear and Kubernetes have route tables today; a request to another
integration's connection is recorded by host, without an action.

### See it with five systems

`examples/demo/run.py` in `toolpass-py` runs the whole picture on your machine, with no accounts:

```sh
cd toolpass-py && pip install -e .
python examples/demo/run.py -open
```

Two people, Dana (a developer) and Sam (support, read-only), call the tools of a `shop` MCP server
through the gateway: `open_pr` (GitHub, Jira, Slack), `ship` (Kubernetes, Slack), `report_bug`
(Linear, Jira, Slack) and `status` (reads). GitHub, Jira, Slack, Linear and Kubernetes are
stand-ins on `127.0.0.1` answering from the grants in `examples/demo/world.py`; the permission
checks are the real integrations'. Dana ships to staging and is refused prod by Kubernetes RBAC;
Sam's writes stop at the first system that says no. The record is kept in a temporary folder, not
yours, and your Claude configuration is not touched. To use the server from Claude Code instead,
add it (`claude mcp add shop -e DEMO_USER=sam@acme.dev -- python examples/demo/shop_server.py`),
run `toolpass gateway on`, and ask Claude to open a pull request.

## Where the record is

One SQLite file, `gateway.db`, in `~/Library/Application Support/toolpass/` (macOS),
`%LOCALAPPDATA%\toolpass\` (Windows) or `~/.local/share/toolpass/` (Linux); `TOOLPASS_DATA_DIR`
moves it. The folder and the file are readable only by you. Arguments are stored shortened (long
strings cut to 200 characters) and results cut to 4,000 characters. Calls older than 90 days are
deleted when the dashboard starts. Nothing is sent anywhere.

## What it cannot see

- **HTTP servers.** Servers reached over HTTP are listed, not recorded yet.
- **Servers started elsewhere.** A server you start by hand, or a client whose config file is not in
  the table above, is not wrapped.
- **A client that rewrites its file from memory.** toolpass checks a file has not changed just before
  replacing it, and starts over when it has, so a client's own save is not lost. A client that later
  saves its whole file from an old copy in memory can drop the wrapped entries; `toolpass gateway
  status` shows it, and `gateway on` wraps them again.

If the record cannot be written (a full disk), the server still runs and the calls still go
through; toolpass says so once on the server's stderr.

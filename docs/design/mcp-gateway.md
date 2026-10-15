# Design: the MCP gateway

**Status: proposal. Nothing on this page is built yet.** It is here to be argued with before any
code is written.

## The goal

You use Claude Desktop, Claude Code or Cursor with MCP servers: GitHub, Slack, a database. You run
one command, keep working exactly as before, and open one page that shows every MCP server your
agents use, every tool call they ever made, from which client, with what arguments, and what came
back.

Two rules shape everything below:

- **Nothing to add.** You never register a server in toolpass. The gateway finds the servers your
  clients already have and puts itself in front of them.
- **Every transport.** A local server started over stdio and a hosted server reached over HTTP are
  both covered, the same way.

## Finding the servers

Every client lists its MCP servers in a config file at a known path:

| Client | File |
|---|---|
| Claude Desktop | `claude_desktop_config.json` in the app's config folder |
| Claude Code | `~/.claude.json` and a project's `.mcp.json` |
| Cursor | `~/.cursor/mcp.json` and a project's `.cursor/mcp.json` |
| Windsurf | `~/.codeium/windsurf/mcp_config.json` |
| VS Code | `mcp.json` in the user settings folder and a workspace's `.vscode/mcp.json` |

`toolpass gateway on` reads those files and rewrites each server entry to go through toolpass,
keeping the original entry beside it. `toolpass gateway off` puts every file back as it was. A
server you add to a client later is picked up the next time the gateway starts, because it scans
the same files again. `toolpass gateway status` lists what it found and what it wraps.

Each server stays itself in the client: GitHub is still a server called GitHub, with its own
toggle. toolpass sits inside each entry rather than merging them into one server with prefixed tool
names, so nothing about how you use the client changes.

## How a call goes through

```text
Claude Desktop / Cursor / Claude Code
        |
        |  stdio: toolpass mcp -- <the original command>
        |  HTTP:  http://127.0.0.1:<port>/mcp/<server>   (upstream: the original URL)
        v
   toolpass  -- tool calls and their results: recorded --> local store
        |
        v
   the real server: a child process, or the hosted URL
```

- **A stdio server** becomes `toolpass mcp -- <original command>`. toolpass starts the real server
  as a child, forwards every message unchanged, and looks at `tools/call` and its result.
- **An HTTP server** gets its URL replaced by the gateway's local address, with the original as the
  upstream. Everything is forwarded, including the OAuth discovery endpoints and the bearer token,
  so signing in to a hosted server works as before. toolpass only looks at the tool calls.
- **The gateway daemon** is `toolpass serve` on localhost, started by the first wrapper that needs
  it. It holds the store and the page, so every session from every client lands
  in one place. A stdio wrapper that cannot reach it keeps working on its own and hands over what it
  saw once the daemon is back.

A tool's schema is passed to the client exactly as the server declares it. The canary test that led
to this design showed why: a wrapper that hid the real arguments made the model call tools wrongly.

## What it does to a call: nothing

The gateway records; it never blocks, holds or changes a call. MCP clients already ask "Allow this
tool?" before a call runs, and a second approval would only ask the same person twice. Each call
is recorded when it is sent and again when its result comes back: server, tool, arguments, result,
success or error, and how long it took.

MCP tools carry annotations (`readOnlyHint`, `destructiveHint`, `openWorldHint`). The page shows
them next to each tool, so a destructive call stands out in the history.

## The page

The live page from `toolpass.watch()` becomes one tab of it.

- **Servers:** what the gateway found, in which client, stdio or HTTP, wrapped or not.
- **Calls:** every tool call ever made, with server, tool, arguments, result, ran or refused and why,
  filtered by server, tool, client, session and time.

It binds to the loopback interface with a token in every path, as the live page does today.

## Storage

One SQLite file in the user's data folder, holding servers and calls. Arguments and
results are stored shortened, as the audit events are today, and an injected credential never
reaches the store. The file is the user's own: nothing is sent anywhere.

## What it cannot do

- A server started outside the clients' config files (by hand, by a script) is not wrapped. The
  page can list it as seen in a client but not wrapped, when the client reports it.
- A client whose config format is unknown is not covered until it is added to the table above.
- For a hosted server that acts with the user's own login, such as GitHub's, the server already
  enforces that user's permissions. There the gateway adds the record, not a permission check.

## Order of work

1. **Stdio and the store.** `toolpass mcp`, the daemon with the SQLite store and the page, and
   `gateway on|off|status` for Claude Desktop and Claude Code. The first real demo: your own Claude
   Desktop, your own GitHub server, and every call it makes on the page as it happens.
2. **HTTP.** Hosted servers with OAuth passthrough, and the other clients in the table.
3. **The history.** Search and filters on the page, by server, tool, client and day.

Each step is one pull request, with its tests and its documentation.

## Open questions

- Should `gateway on` run once, or keep a watcher that wraps a server the moment it appears in a
  client's file?
- Where does the daemon run when no client is open: on demand only, or as a login item?
- Do teams want one gateway shared across machines, with sign-in on the page, or is that a later
  step?

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

A wrapped entry starts `python -m toolpass.cli mcp` with the original command after `--`, using the
Python toolpass was installed in, and keeps the original under a `"toolpass"` key in the same entry;
the server's `env` is untouched. The first change to a file leaves a copy of the original beside it,
named `<file>.toolpass-backup`. A file that is not valid JSON is left alone.

## The dashboard

```sh
toolpass dashboard -open
```

It prints an address on `127.0.0.1` with a random token in the path, and serves until Ctrl-C.
**Calls** lists every tool call, newest first, with the client, the server, the tool, the
arguments, the result (click to open) and how long it took; a call shows up when it is sent and
updates when the result arrives. The filter box matches any text in a call. **Servers** lists what
the gateway found, whether it is being recorded, how many calls it had, and its tools, marked
read-only or destructive as the server declares them.

Each time it starts, the dashboard wraps servers added to the clients since the last run;
`-no-wrap` turns that off. `-listen 127.0.0.1:8899` picks the port. The page binds to the loopback
interface only and is read-only.

## Where the record is

One SQLite file, `gateway.db`, in `~/Library/Application Support/toolpass/` (macOS),
`%LOCALAPPDATA%\toolpass\` (Windows) or `~/.local/share/toolpass/` (Linux); `TOOLPASS_DATA_DIR`
moves it. The folder and the file are readable only by you. Arguments are stored shortened (long
strings cut to 200 characters) and results cut to 4,000 characters. Nothing is sent anywhere.

## What it cannot see

- **HTTP servers.** Servers reached over HTTP are listed, not recorded yet.
- **Servers started elsewhere.** A server you start by hand, or a client whose config file is not in
  the table above, is not wrapped.
- **A server whose client rewrites its file.** If a client rewrites its config file from memory while
  `gateway on` runs, the change can be lost; run it with the clients closed, then check
  `toolpass gateway status`.

If the record cannot be written (a full disk), the server still runs and the calls still go
through; toolpass says so once on the server's stderr.

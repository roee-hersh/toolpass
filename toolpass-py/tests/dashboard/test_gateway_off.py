"""Entries toolpass 0.8 and 0.9 rewrote (`toolpass gateway on`) keep working
until `toolpass gateway off` puts them back exactly as they were."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path

from toolpass import _gateway, cli
from toolpass._gateway import ClientFile

ORIGINAL = {
    "mcpServers": {
        "github": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-github"], "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "ghp_x"}},
        "bare": {"command": "my-server"},
        "remote": {"type": "http", "url": "https://api.githubcopilot.com/mcp/", "headers": {"Authorization": "Bearer ${GH}"}},
        "plain": {"command": "untouched"},
    },
    "projects": {"/home/dana/app": {"mcpServers": {"db": {"command": "postgres-mcp", "args": ["--ro"]}}}},
    "numStartups": 3,
}

WRAP = ["/usr/bin/python3", "-P", "-m", "toolpass.cli", "mcp", "-server"]
REWRITTEN = {
    "mcpServers": {
        "github": {
            "command": WRAP[0],
            "args": [*WRAP[1:], "github", "-client", "claude-code", "-config", "x", "--", "npx", "-y", "@modelcontextprotocol/server-github"],
            "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "ghp_x"},
            "toolpass": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-github"]},
        },
        "bare": {"command": WRAP[0], "args": [*WRAP[1:], "bare", "--", "my-server"], "toolpass": {"command": "my-server"}},
        "remote": {
            "type": "stdio",
            "command": WRAP[0],
            "args": [*WRAP[1:], "remote", "-url", "https://api.githubcopilot.com/mcp/"],
            "env": {"TOOLPASS_MCP_HEADERS": '{"Authorization": "Bearer ${GH}"}'},
            "toolpass": {"type": "http", "url": "https://api.githubcopilot.com/mcp/", "headers": {"Authorization": "Bearer ${GH}"}},
        },
        "plain": {"command": "untouched"},
    },
    "projects": {
        "/home/dana/app": {
            "mcpServers": {"db": {"command": WRAP[0], "args": ["--", "postgres-mcp", "--ro"], "toolpass": {"command": "postgres-mcp", "args": ["--ro"]}}}
        }
    },
    "numStartups": 3,
}


def test_off_puts_every_entry_back(tmp_path: Path) -> None:
    path = tmp_path / ".claude.json"
    path.write_text(json.dumps(REWRITTEN))
    if os.name != "nt":
        os.chmod(path, 0o600)
    done = _gateway.off([ClientFile("claude-code", path)])
    assert sorted(r.name for r in done) == ["bare", "db", "github", "remote"]
    assert json.loads(path.read_text()) == ORIGINAL
    if os.name != "nt":
        assert os.stat(path).st_mode & 0o777 == 0o600
    assert _gateway.off([ClientFile("claude-code", path)]) == [], "nothing left to put back"


def test_unreadable_files_are_left_alone(tmp_path: Path) -> None:
    bad = tmp_path / "c.json"
    bad.write_text("{not json")
    assert _gateway.off([ClientFile("claude-desktop", bad)]) == []
    assert bad.read_text() == "{not json"


def test_the_gateway_command(monkeypatch: object) -> None:
    out, err = io.StringIO(), io.StringIO()
    assert cli.gateway(["on"], out, err) == 2 and "no longer wraps" in err.getvalue()


def test_a_rewritten_entry_still_starts_its_server() -> None:
    """`toolpass mcp ... -- COMMAND` runs COMMAND with the client's stdio, unrecorded."""
    echo = [sys.executable, "-c", "import sys; sys.stdout.write(sys.stdin.read().upper())"]
    done = subprocess.run(
        [sys.executable, "-m", "toolpass.cli", "mcp", "-server", "x", "-client", "c", "--", *echo], input=b"ping\n", capture_output=True, timeout=60
    )
    assert done.returncode == 0 and done.stdout == b"PING\n"
    assert b"gateway off" in done.stderr

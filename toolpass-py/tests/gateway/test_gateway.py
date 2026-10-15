"""`toolpass gateway`: the clients' own config files are found, every stdio
server is wrapped, and `off` puts each file back as it was."""

from __future__ import annotations

import io
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from toolpass import _gateway, cli
from toolpass._gateway import ClientFile
from toolpass._store import Store

DESKTOP = {
    "mcpServers": {
        "github": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-github"], "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "ghp_x"}},
        "fs": {"command": "uvx", "args": ["mcp-server-filesystem", "/tmp"]},
        "bare": {"command": "my-server"},
        "linear": {"type": "http", "url": "https://mcp.linear.app/mcp"},
        "broken": "not a mapping",
    },
    "globalShortcut": "Ctrl+Space",
}

CODE = {
    "numStartups": 12,
    "mcpServers": {"sentry": {"type": "stdio", "command": "npx", "args": ["@sentry/mcp-server"]}},
    "projects": {
        "/home/dana/app": {"mcpServers": {"db": {"command": "postgres-mcp", "args": ["--ro"]}}, "allowedTools": []},
        "/home/dana/other": {"allowedTools": []},
    },
}


@pytest.fixture
def files(tmp_path: Path) -> list[ClientFile]:
    desktop = tmp_path / "claude_desktop_config.json"
    desktop.write_text(json.dumps(DESKTOP, indent=2))
    code = tmp_path / ".claude.json"
    code.write_text(json.dumps(CODE))
    if os.name != "nt":
        os.chmod(code, 0o600)
    return [ClientFile("claude-desktop", desktop), ClientFile("claude-code", code)]


def _load(f: ClientFile) -> Any:
    return json.loads(f.path.read_text())


def test_scan_changes_nothing(files: list[ClientFile]) -> None:
    before = [f.path.read_bytes() for f in files]
    found = _gateway.scan(files)
    assert [f.path.read_bytes() for f in files] == before
    names = {(f.client, f.name): f for f in found}
    assert set(names) == {("claude-desktop", n) for n in ("github", "fs", "bare", "linear")} | {("claude-code", "sentry"), ("claude-code", "db")}
    assert names[("claude-desktop", "linear")].transport == "http" and not names[("claude-desktop", "linear")].wrapped
    assert names[("claude-code", "db")].config.endswith("(/home/dana/app)")
    assert not any(f.wrapped for f in found)


def test_on_wraps_stdio_servers_and_off_restores_every_byte_that_matters(files: list[ClientFile]) -> None:
    original = [_load(f) for f in files]
    found = _gateway.on(files)
    assert all(f.wrapped for f in found if f.transport == "stdio")
    desk = _load(files[0])
    gh = desk["mcpServers"]["github"]
    launcher = _gateway._launcher()
    assert [gh["command"], *gh["args"]] == [
        *launcher,
        "mcp",
        "-server",
        "github",
        "-client",
        "claude-desktop",
        "-config",
        str(files[0].path),
        "--",
        "npx",
        "-y",
        "@modelcontextprotocol/server-github",
    ]
    assert gh["env"] == {"GITHUB_PERSONAL_ACCESS_TOKEN": "ghp_x"}, "the server's environment is kept"
    assert gh["toolpass"] == {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-github"]}
    assert desk["mcpServers"]["linear"] == DESKTOP["mcpServers"]["linear"], "HTTP servers are left alone"
    assert desk["mcpServers"]["broken"] == "not a mapping" and desk["globalShortcut"] == "Ctrl+Space"
    code = _load(files[1])
    assert code["projects"]["/home/dana/app"]["mcpServers"]["db"]["args"][-3:] == ["--", "postgres-mcp", "--ro"]
    assert code["numStartups"] == 12
    for f in files:
        assert f.path.with_name(f.path.name + ".toolpass-backup").read_bytes(), "a backup of the original, beside it"
    if os.name != "nt":
        assert stat.S_IMODE(os.stat(files[1].path).st_mode) == 0o600, "the file's mode is kept"

    again = [f.path.read_bytes() for f in files]
    _gateway.on(files)
    assert [f.path.read_bytes() for f in files] == again, "on twice wraps nothing twice"

    _gateway.off(files)
    assert [_load(f) for f in files] == original
    assert "args" not in _load(files[0])["mcpServers"]["bare"], "an entry without args gets none back"


def test_off_keeps_the_first_backup(files: list[ClientFile]) -> None:
    first = files[0].path.read_bytes()
    _gateway.on(files)
    _gateway.off(files)
    _gateway.on(files)
    assert files[0].path.with_name(files[0].path.name + ".toolpass-backup").read_bytes() == first


def test_a_client_rewriting_its_file_meanwhile_keeps_its_change(files: list[ClientFile], monkeypatch: pytest.MonkeyPatch) -> None:
    real = _gateway._visit_file
    raced = []

    def racing(cf: ClientFile, change: str | None) -> list[_gateway.Found]:
        if not raced and cf.client == "claude-code":
            raced.append(1)
            doc = _load(cf)
            original_read = cf.path.read_bytes

            def read_then_client_writes(self: Path = cf.path) -> bytes:
                data = original_read()
                doc["numStartups"] = 13  # the client saves its own state in between
                cf.path.write_text(json.dumps(doc))
                monkeypatch.undo()
                return data

            monkeypatch.setattr(type(cf.path), "read_bytes", lambda self: read_then_client_writes() if self == cf.path else original_read.__func__(self))  # type: ignore[attr-defined]
        return real(cf, change)

    monkeypatch.setattr(_gateway, "_visit_file", racing)
    _gateway.on(files)
    code = _load(files[1])
    assert code["numStartups"] == 13, "the client's write survived"
    assert "toolpass" in code["mcpServers"]["sentry"], "and the entry was wrapped on the second try"


def test_a_wrapper_whose_python_is_gone_is_reported(files: list[ClientFile]) -> None:
    _gateway.on(files)
    doc = _load(files[0])
    doc["mcpServers"]["github"]["command"] = "/no/such/python"
    files[0].path.write_text(json.dumps(doc))
    broken = {f.name: f.broken for f in _gateway.scan(files)}
    assert broken["github"] and "gone" in broken["github"] and broken["fs"] is None


def test_the_wrapper_never_imports_from_the_clients_working_directory(tmp_path: Path) -> None:
    """A client starts a server in the folder the user opened; a planted toolpass/ there must not run."""
    planted = tmp_path / "repo"
    (planted / "toolpass").mkdir(parents=True)
    (planted / "toolpass" / "__init__.py").write_text("")
    (planted / "toolpass" / "cli.py").write_text("print('PLANTED')\n")
    (planted / "json.py").write_text("print('PLANTED json')\n")
    done = subprocess.run([*_gateway._launcher(), "version"], cwd=planted, capture_output=True, text=True, timeout=60)
    assert "PLANTED" not in done.stdout + done.stderr
    assert done.returncode == 0 and done.stdout.strip()


def test_unreadable_files_are_left_alone(tmp_path: Path) -> None:
    bad = tmp_path / "c.json"
    bad.write_text("{not json")
    assert _gateway.on([ClientFile("claude-desktop", bad)]) == []
    assert bad.read_text() == "{not json"


def test_found_servers_go_to_the_store(files: list[ClientFile], tmp_path: Path) -> None:
    store = Store(tmp_path / "g.db")
    _gateway.on(files, store=store)
    servers = {(s["client"], s["name"]): s for s in store.servers()}
    assert servers[("claude-desktop", "github")]["wrapped"] is True
    assert servers[("claude-desktop", "github")]["target"] == "npx -y @modelcontextprotocol/server-github"
    assert servers[("claude-desktop", "linear")]["transport"] == "http"


def test_client_files_on_linux(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    assert _gateway.client_files(tmp_path) == []
    (tmp_path / ".config" / "Claude").mkdir(parents=True)
    (tmp_path / ".config" / "Claude" / "claude_desktop_config.json").write_text("{}")
    (tmp_path / ".claude.json").write_text("{}")
    assert [f.client for f in _gateway.client_files(tmp_path)] == ["claude-desktop", "claude-code"]


def test_client_files_on_windows_and_macos(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    (tmp_path / "Roaming" / "Claude").mkdir(parents=True)
    (tmp_path / "Roaming" / "Claude" / "claude_desktop_config.json").write_text("{}")
    assert [f.client for f in _gateway.client_files(tmp_path)] == ["claude-desktop"]
    monkeypatch.setattr(sys, "platform", "darwin")
    mac = tmp_path / "Library" / "Application Support" / "Claude"
    mac.mkdir(parents=True)
    (mac / "claude_desktop_config.json").write_text("{}")
    assert _gateway.client_files(tmp_path)[0].path == mac / "claude_desktop_config.json"


def test_gateway_command(files: list[ClientFile], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TOOLPASS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(_gateway, "client_files", lambda home=None: files)
    out, err = io.StringIO(), io.StringIO()
    assert cli.run(["gateway", "on"], out, err) == 0
    assert "github" in out.getvalue() and "recording" in out.getvalue() and "restart your MCP clients" in out.getvalue()
    out = io.StringIO()
    assert cli.run(["gateway", "status"], out, err) == 0 and "recording" in out.getvalue()
    out = io.StringIO()
    assert cli.run(["gateway", "off"], out, err) == 0 and "not wrapped" in out.getvalue()
    assert cli.run(["gateway"], out, err) == 2 and "usage" in err.getvalue()

"""``toolpass gateway``: find the MCP servers your clients already use and put
toolpass in front of each, with nothing to register.

Each client keeps its servers in a config file at a known place. ``on()``
rewrites every server entry in those files to run through ``toolpass mcp``,
keeping the original in the same entry under a ``"toolpass"`` key; ``off()``
puts every entry back. A stdio server runs as toolpass's child; an HTTP server
(Streamable HTTP) is reached by ``toolpass mcp -url`` with the entry's own
headers, once a probe shows it answers to them (a server that signs in with
OAuth is left as it is). Nothing else in the files changes, and each file is
backed up once, beside itself, before the first change.
"""

from __future__ import annotations

import contextlib
import copy
import dataclasses
import json
import os
import shutil
import sys
import sysconfig
import tempfile
from collections.abc import Iterator, MutableMapping
from pathlib import Path
from typing import Any

from toolpass import _mcp_http
from toolpass._store import Store

__all__ = ["ClientFile", "Found", "client_files", "off", "on", "scan"]

_KEY = "toolpass"
_BACKUP_SUFFIX = ".toolpass-backup"


@dataclasses.dataclass(frozen=True)
class ClientFile:
    """One client's config file."""

    client: str  # claude-desktop, claude-code
    path: Path


@dataclasses.dataclass
class Found:
    """One server entry in a client's file."""

    client: str
    config: str  # the file, and for Claude Code the project it belongs to
    name: str
    transport: str  # stdio, http
    target: str  # the command line, or the URL
    wrapped: bool
    broken: str | None = None  # why a wrapped entry cannot start, when it cannot
    why: str | None = None  # why an entry is not wrapped, when toolpass chose not to


def client_files(home: Path | None = None) -> list[ClientFile]:
    """The config files of the clients toolpass knows, wherever they exist."""
    home = home or Path.home()
    if sys.platform == "darwin":
        desktop = home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    elif sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        desktop = (Path(appdata) if appdata else home / "AppData" / "Roaming") / "Claude" / "claude_desktop_config.json"
    else:
        desktop = home / ".config" / "Claude" / "claude_desktop_config.json"
    return [f for f in (ClientFile("claude-desktop", desktop), ClientFile("claude-code", home / ".claude.json")) if f.path.is_file()]


def _server_maps(client: ClientFile, doc: Any) -> Iterator[tuple[str, MutableMapping[str, Any]]]:
    """Every ``mcpServers`` mapping in a client's document, with a label for
    where it is (Claude Code also keeps one per project)."""
    if not isinstance(doc, MutableMapping):
        return
    servers = doc.get("mcpServers")
    if isinstance(servers, MutableMapping):
        yield str(client.path), servers
    if client.client == "claude-code":
        projects = doc.get("projects")
        if isinstance(projects, MutableMapping):
            for project, cfg in projects.items():
                if isinstance(cfg, MutableMapping) and isinstance(cfg.get("mcpServers"), MutableMapping):
                    yield f"{client.path} ({project})", cfg["mcpServers"]


def _transport(entry: Any) -> str | None:
    if not isinstance(entry, MutableMapping):
        return None
    kind = entry.get("type")
    saved = entry.get(_KEY)
    if isinstance(saved, MutableMapping):
        return "http" if isinstance(saved.get("url"), str) else "stdio"
    if isinstance(entry.get("command"), str) and kind in (None, "stdio"):
        return "stdio"
    if isinstance(entry.get("url"), str) or kind in ("http", "sse", "streamable-http"):
        return "http"
    return None


def _original(entry: MutableMapping[str, Any]) -> tuple[str, list[str]]:
    saved = entry.get(_KEY)
    if isinstance(saved, MutableMapping):
        return str(saved.get("command", "")), [str(a) for a in saved.get("args", []) or []]
    return str(entry.get("command", "")), [str(a) for a in entry.get("args", []) or []]


def _launcher() -> list[str]:
    """How a client starts toolpass: from the installation that wrapped it,
    wherever the client's PATH points, and never importing code from the
    client's working directory (``python -m`` puts it first on the module
    path, so a repository the user opened could stand in for toolpass or the
    standard library)."""
    if sys.version_info >= (3, 11):
        return [sys.executable, "-P", "-m", "toolpass.cli"]  # -P: no working directory on sys.path
    script = Path(sysconfig.get_path("scripts")) / ("toolpass.exe" if sys.platform == "win32" else "toolpass")
    if script.is_file():
        return [str(script)]  # a console script's sys.path starts at its own folder
    # A source checkout on 3.10: drop the working directory before importing toolpass.
    return [sys.executable, "-c", "import sys; sys.path[:] = [p for p in sys.path if p not in ('', '.')]; from toolpass.cli import main; main()"]


def _read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


class _Changed(Exception):
    """The file changed on disk while toolpass was rewriting it."""


def _write(path: Path, doc: Any, read: bytes) -> None:
    """Write ``doc`` as JSON over ``path`` atomically, keeping the file's mode,
    after a one-time backup of the original beside it. Raises ``_Changed``,
    writing nothing, when the file no longer holds ``read``: a client that
    rewrote it in the meantime keeps its change."""
    if path.read_bytes() != read:
        raise _Changed(str(path))
    backup = path.with_name(path.name + _BACKUP_SUFFIX)
    if not backup.exists():
        shutil.copy2(path, backup)
    mode = path.stat().st_mode & 0o777
    fd, tmp = tempfile.mkstemp(prefix=".toolpass-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(doc, f, indent=2, ensure_ascii=False)
            f.write("\n")
        with contextlib.suppress(OSError):
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _visit(files: list[ClientFile], change: str | None, store: Store | None) -> list[Found]:
    found: list[Found] = []
    for cf in files:
        for _ in range(3):  # a client rewriting its file mid-change: read it again
            try:
                found_here = _visit_file(cf, change)
            except _Changed:
                continue
            except (OSError, ValueError):
                found_here = []  # unreadable or not JSON: leave it alone
            found.extend(found_here)
            break
        else:
            print(f"toolpass: {cf.path} kept changing while toolpass rewrote it; left as it is, try again", file=sys.stderr)
    if store is not None:
        for f in found:
            store.record_server(f.client, f.name, f.config, f.transport, f.target, f.wrapped)
        store.forget_servers([str(cf.path) for cf in files], [(f.client, f.config, f.name) for f in found])
    return found


def _visit_file(cf: ClientFile, change: str | None) -> list[Found]:
    found: list[Found] = []
    launcher = _launcher()
    read = cf.path.read_bytes()
    doc = json.loads(read.decode("utf-8"))
    before = copy.deepcopy(doc)
    for where, servers in _server_maps(cf, doc):
        for name, entry in list(servers.items()):
            transport = _transport(entry)
            if transport is None or not isinstance(entry, MutableMapping):
                continue
            if transport == "http":
                found.append(_visit_http(cf, where, str(name), entry, change, launcher))
                continue
            command, args = _original(entry)
            wrapped = isinstance(entry.get(_KEY), MutableMapping)
            if change == "on" and not wrapped and command:
                entry[_KEY] = {"command": command, "args": args} if "args" in entry else {"command": command}
                entry["command"] = launcher[0]
                entry["args"] = [*launcher[1:], "mcp", "-server", str(name), "-client", cf.client, "-config", where, "--", command, *args]
                wrapped = True
            elif change == "off" and wrapped:
                saved = entry.pop(_KEY)
                entry["command"] = command
                if "args" in saved:
                    entry["args"] = args
                else:
                    entry.pop("args", None)
                wrapped = False
            broken = None
            if wrapped and not Path(str(entry.get("command", ""))).is_file():
                broken = f"the Python that wrapped it is gone ({entry.get('command')}); run `toolpass gateway off`, then `on`, from a working install"
            found.append(Found(cf.client, where, str(name), "stdio", " ".join([command, *args]), wrapped, broken))
    if change is not None and doc != before:
        _write(cf.path, doc, read)
    return found


_WRAPPABLE = (None, "http", "streamable-http")  # "sse" is the older transport, not carried


def _visit_http(cf: ClientFile, where: str, name: str, entry: MutableMapping[str, Any], change: str | None, launcher: list[str]) -> Found:
    saved = entry.get(_KEY)
    wrapped = isinstance(saved, MutableMapping)
    original: dict[str, Any] = dict(saved if isinstance(saved, MutableMapping) else entry)  # a copy: entry is rewritten below
    url = str(original.get("url", ""))
    why = None
    if change == "on" and not wrapped:
        raw = original.get("headers")
        headers = {str(k): str(v) for k, v in raw.items()} if isinstance(raw, MutableMapping) else {}
        if original.get("type") not in _WRAPPABLE:
            why = f"{original.get('type')} transport, not recorded"
        elif not url.startswith(("https://", "http://")):
            why = "not an http(s) URL"
        else:
            state = _mcp_http.probe(_mcp_http.expand(url), {k: _mcp_http.expand(v) for k, v in headers.items()})
            if state == "sign-in":
                why = "signs in with OAuth, which toolpass does not carry; not recorded"
            elif state == "unreachable":
                why = "did not answer when toolpass looked; `toolpass gateway on` tries again"
            else:
                keep = {k: v for k, v in entry.items() if k not in ("type", "url", "headers")}
                entry.clear()
                entry.update(keep)
                entry[_KEY] = {k: v for k, v in original.items() if k in ("type", "url", "headers")}
                entry["type"] = "stdio"
                entry["command"] = launcher[0]
                entry["args"] = [*launcher[1:], "mcp", "-server", name, "-client", cf.client, "-config", where, "-url", url]
                if headers:
                    env = entry.get("env")
                    entry["env"] = {**(env if isinstance(env, MutableMapping) else {}), _mcp_http.HEADERS_ENV: json.dumps(headers)}
                wrapped = True
    elif change == "off" and wrapped:
        restore = dict(entry.pop(_KEY))
        for k in ("type", "command", "args"):
            entry.pop(k, None)
        env = entry.get("env")
        if isinstance(env, MutableMapping):
            env.pop(_mcp_http.HEADERS_ENV, None)
            if not env:
                entry.pop("env")
        entry.update(restore)
        wrapped = False
    broken = None
    if wrapped and not Path(str(entry.get("command", ""))).is_file():
        broken = f"the Python that wrapped it is gone ({entry.get('command')}); run `toolpass gateway off`, then `on`, from a working install"
    return Found(cf.client, where, name, "http", url, wrapped, broken, why)


def scan(files: list[ClientFile] | None = None, store: Store | None = None) -> list[Found]:
    """The servers in the clients' files, changing nothing."""
    return _visit(client_files() if files is None else files, None, store)


def on(files: list[ClientFile] | None = None, store: Store | None = None) -> list[Found]:
    """Wrap every server not wrapped yet; returns every server found."""
    return _visit(client_files() if files is None else files, "on", store)


def off(files: list[ClientFile] | None = None, store: Store | None = None) -> list[Found]:
    """Put every wrapped server back as it was."""
    return _visit(client_files() if files is None else files, "off", store)

"""``toolpass gateway off``: put back the MCP server entries an earlier
toolpass (0.8 and 0.9, ``toolpass gateway on``) rewrote to run through
``toolpass mcp``.

Each rewritten entry kept its original under a ``"toolpass"`` key; ``off()``
puts it back and leaves everything else in the file as it is. The file is
written atomically, keeping its mode, and only if no one changed it while
toolpass was reading it.
"""

from __future__ import annotations

import contextlib
import copy
import dataclasses
import json
import os
import sys
import tempfile
from collections.abc import Iterator, MutableMapping
from pathlib import Path
from typing import Any

__all__ = ["ClientFile", "Restored", "client_files", "off"]

_KEY = "toolpass"
_HEADERS_ENV = "TOOLPASS_MCP_HEADERS"


@dataclasses.dataclass(frozen=True)
class ClientFile:
    """One client's config file."""

    client: str  # claude-desktop, claude-code
    path: Path


@dataclasses.dataclass(frozen=True)
class Restored:
    """One server entry put back."""

    client: str
    config: str  # the file, and for Claude Code the project it belongs to
    name: str


def client_files(home: Path | None = None) -> list[ClientFile]:
    """The config files of the clients ``gateway on`` rewrote, wherever they exist."""
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


def _restore(entry: MutableMapping[str, Any]) -> None:
    saved = dict(entry.pop(_KEY))
    if isinstance(saved.get("url"), str):  # an HTTP server, run through `toolpass mcp -url`
        for k in ("type", "command", "args"):
            entry.pop(k, None)
        env = entry.get("env")
        if isinstance(env, MutableMapping):
            env.pop(_HEADERS_ENV, None)
            if not env:
                entry.pop("env")
        entry.update(saved)
        return
    entry["command"] = saved.get("command", "")
    if "args" in saved:
        entry["args"] = saved["args"]
    else:
        entry.pop("args", None)


class _Changed(Exception):
    """The file changed on disk while toolpass was rewriting it."""


def _write(path: Path, doc: Any, read: bytes) -> None:
    if path.read_bytes() != read:
        raise _Changed(str(path))
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


def _off_file(cf: ClientFile) -> list[Restored]:
    read = cf.path.read_bytes()
    doc = json.loads(read.decode("utf-8"))
    before = copy.deepcopy(doc)
    done: list[Restored] = []
    for where, servers in _server_maps(cf, doc):
        for name, entry in servers.items():
            if isinstance(entry, MutableMapping) and isinstance(entry.get(_KEY), MutableMapping):
                _restore(entry)
                done.append(Restored(cf.client, where, str(name)))
    if doc != before:
        _write(cf.path, doc, read)
    return done


def off(files: list[ClientFile] | None = None) -> list[Restored]:
    """Put every rewritten entry back; returns the entries put back."""
    done: list[Restored] = []
    for cf in client_files() if files is None else files:
        for _ in range(3):  # a client rewriting its file meanwhile: read it again
            try:
                done.extend(_off_file(cf))
            except _Changed:
                continue
            except (OSError, ValueError):
                pass  # unreadable or not JSON: leave it alone
            break
        else:
            print(f"toolpass: {cf.path} kept changing while toolpass rewrote it; left as it is, try again", file=sys.stderr)
    return done

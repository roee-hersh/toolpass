"""Entitlements: the actions a tool is allowed to perform, recorded from a run.

A tool's entitlements are data, in ``toolpass-tools.yaml`` next to the
tools (or a mapping given in code):

    tools:
      propose_fix:
        hosts: []                      # other hosts the body may call
        actions:
          - action: github:repo.read
            resource: "repo:{repo}"
            as: agent                  # on the agent's own authority
            effect: read
          - action: github:pr.create
            resource: "repo:{repo}"
            as: user                   # checked against the session's user
            effect: write
            approve: false

``resource`` is a template over the tool's arguments (``{repo}``) or a glob
(``repo:acme/*``). Record a tool's entitlements by running it with
``toolpass.configure(record="toolpass-tools.yaml")``: every action its body
performed is written there, with argument values turned back into
templates. In production, ``configure(entitlements="toolpass-tools.yaml")``
checks the recorded ``as: user`` actions before the body runs and refuses
anything the body does beyond the list.
"""

from __future__ import annotations

import dataclasses
import fnmatch
import os
import re
import tempfile
import threading
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

import yaml

from toolpass._checks import _glob
from toolpass._context import ObservedAction
from toolpass.core.routes import EFFECTS, stronger, template_fields

__all__ = ["ActionRecorder", "Entitlement", "Entitlements", "ToolEntitlements"]

_NAME_RE = re.compile(r"[a-z][a-z0-9]*:[A-Za-z0-9_.:*-]+")
_FIELD_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


class EntitlementsError(ValueError):
    """The entitlements file or mapping is not usable."""


@dataclasses.dataclass(frozen=True)
class Entitlement:
    """One action a tool may perform."""

    action: str
    resource: str
    as_: str = "user"
    effect: str = "write"
    approve: bool = False

    def __post_init__(self) -> None:
        if not _NAME_RE.fullmatch(self.action):
            raise EntitlementsError(f"action {self.action!r} must be <integration>:<action>, such as github:pr.create")
        if self.as_ not in ("user", "agent"):
            raise EntitlementsError(f"{self.action}: 'as' must be user or agent, not {self.as_!r}")
        if self.effect not in EFFECTS:
            raise EntitlementsError(f"{self.action}: effect must be one of {', '.join(EFFECTS)}, not {self.effect!r}")

    @property
    def integration(self) -> str:
        return self.action.split(":", 1)[0]

    @property
    def name(self) -> str:
        return self.action.split(":", 1)[1]

    def fields(self) -> tuple[str, ...]:
        return template_fields(self.resource)

    def fill(self, arguments: Mapping[str, Any]) -> str | None:
        """The resource for these arguments; None when a field is missing or
        not a plain string or integer (a bool is neither)."""
        values: dict[str, str] = {}
        for f in self.fields():
            if f not in arguments:
                return None
            v = arguments[f]
            if type(v) not in (str, int):
                return None
            values[f] = str(v)
        return _FIELD_RE.sub(lambda m: values[m.group(1)], self.resource)

    def matches(self, resource: str, arguments: Mapping[str, Any]) -> bool:
        """Whether an observed resource is one this entitlement covers: the
        template filled from the arguments, compared exactly or as a glob.
        A template whose argument the call does not have covers nothing."""
        filled = self.fill(arguments)
        if filled is None:
            return False
        if not any(c in filled for c in "*?["):
            return filled == resource
        try:
            return _glob(filled).fullmatch(resource) is not None
        except re.error:
            return False

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"action": self.action, "resource": self.resource, "as": self.as_, "effect": self.effect}
        if self.approve:
            out["approve"] = True
        return out

    @classmethod
    def from_dict(cls, d: Mapping[str, Any], where: str) -> Entitlement:
        if not isinstance(d, Mapping):
            raise EntitlementsError(f"{where}: each action must be a mapping")
        unknown = set(d) - {"action", "resource", "as", "effect", "approve"}
        if unknown:
            raise EntitlementsError(f"{where}: unknown keys {', '.join(sorted(unknown))}")
        action, resource = d.get("action"), d.get("resource", "")
        if not isinstance(action, str) or not isinstance(resource, str):
            raise EntitlementsError(f"{where}: action and resource must be strings")
        as_ = d.get("as", "user")
        effect = d.get("effect", "write")
        approve = d.get("approve", False)
        if not isinstance(as_, str) or not isinstance(effect, str) or not isinstance(approve, bool):
            raise EntitlementsError(f"{where}: 'as' and effect must be strings, approve a boolean")
        try:
            return cls(action, resource, as_, effect, approve)
        except EntitlementsError as e:
            raise EntitlementsError(f"{where}: {e}") from None


@dataclasses.dataclass(frozen=True)
class ToolEntitlements:
    """Everything one tool may do."""

    tool: str
    actions: tuple[Entitlement, ...] = ()
    hosts: tuple[str, ...] = ()

    def effect(self) -> str | None:
        """The strongest recorded effect, or None without actions."""
        out: str | None = None
        for a in self.actions:
            out = stronger(out, a.effect)
        return out

    def find(self, name: str, resource: str | None, arguments: Mapping[str, Any]) -> Entitlement | None:
        """The entitlement covering an observed action, or None."""
        for a in self.actions:
            if a.action != name:
                continue
            if resource is None or a.matches(resource, arguments):
                return a
        return None

    def allows_host(self, host: str) -> bool:
        return any(fnmatch.fnmatchcase(host.lower(), h.lower()) for h in self.hosts)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.hosts:
            out["hosts"] = list(self.hosts)
        out["actions"] = [a.to_dict() for a in self.actions]
        return out

    @classmethod
    def from_dict(cls, tool: str, d: Mapping[str, Any]) -> ToolEntitlements:
        if not isinstance(d, Mapping):
            raise EntitlementsError(f"tools.{tool}: must be a mapping")
        unknown = set(d) - {"hosts", "actions"}
        if unknown:
            raise EntitlementsError(f"tools.{tool}: unknown keys {', '.join(sorted(unknown))}")
        hosts = d.get("hosts") or []
        actions = d.get("actions") or []
        if not isinstance(hosts, list) or not all(isinstance(h, str) for h in hosts):
            raise EntitlementsError(f"tools.{tool}: hosts must be a list of strings")
        if not isinstance(actions, list):
            raise EntitlementsError(f"tools.{tool}: actions must be a list")
        return cls(tool, tuple(Entitlement.from_dict(a, f"tools.{tool}.actions[{i}]") for i, a in enumerate(actions)), tuple(hosts))


class Entitlements:
    """The entitlements of a set of tools, by tool name."""

    def __init__(self, tools: Mapping[str, ToolEntitlements] | None = None) -> None:
        self._tools: dict[str, ToolEntitlements] = dict(tools or {})

    @classmethod
    def from_mapping(cls, doc: Mapping[str, Any], where: str = "<entitlements>") -> Entitlements:
        if not isinstance(doc, Mapping):
            raise EntitlementsError(f"{where}: must be a mapping with a 'tools' key")
        tools = doc.get("tools", {})
        if tools is None:
            tools = {}
        if not isinstance(tools, Mapping):
            raise EntitlementsError(f"{where}: tools must be a mapping of tool name to entitlements")
        return cls({str(name): ToolEntitlements.from_dict(str(name), spec) for name, spec in tools.items()})

    @classmethod
    def load(cls, path: str | os.PathLike[str]) -> Entitlements:
        """Read a YAML file. A missing file is an empty set."""
        try:
            with open(path, encoding="utf-8") as f:
                doc = yaml.safe_load(f)
        except FileNotFoundError:
            return cls()
        except yaml.YAMLError as e:
            raise EntitlementsError(f"{os.fspath(path)}: {e}") from None
        return cls.from_mapping(doc or {}, os.fspath(path))

    def for_tool(self, name: str) -> ToolEntitlements | None:
        return self._tools.get(name)

    def tools(self) -> list[str]:
        return sorted(self._tools)

    def to_mapping(self) -> dict[str, Any]:
        return {"tools": {name: self._tools[name].to_dict() for name in sorted(self._tools)}}

    def __len__(self) -> int:
        return len(self._tools)


def _template(resource: str, arguments: Mapping[str, Any]) -> str:
    """``resource`` with argument values put back as ``{name}``: a value that
    is the whole id, or one ``/``-separated segment of it. Longer values
    first, so ``acme/x`` wins over ``x``. Only plain strings and integers
    take part; the result stays literal when no argument matches."""
    if ":" not in resource:
        return resource
    typ, _, ident = resource.partition(":")
    at = ""
    if "@" in ident:
        ident, _, branch = ident.partition("@")
        at = "@" + branch
    candidates = sorted(
        ((str(v), k) for k, v in arguments.items() if type(v) in (str, int) and str(v)),
        key=lambda kv: (-len(kv[0]), kv[1]),
    )
    for value, name in candidates:
        if ident == value:
            ident = f"{{{name}}}"
            break
    else:
        segs = ident.split("/")
        for value, name in candidates:
            if "/" in value:
                continue
            if value in segs:
                segs = [f"{{{name}}}" if s == value else s for s in segs]
        ident = "/".join(segs)
    if at:
        for value, name in candidates:
            if at[1:] == value:
                at = f"@{{{name}}}"
                break
    return f"{typ}:{ident}{at}"


class ActionRecorder:
    """Writes the actions tools performed to an entitlements file.

    Every call's allowed actions are merged into the file under the tool's
    name, with argument values templated back. The file is rewritten
    atomically under a lock; a secret never enters it (hosts, actions and
    resource ids only)."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = os.fspath(path)
        self._lock = threading.Lock()

    def record(self, tool: str, arguments: Mapping[str, Any], observed: Iterable[ObservedAction]) -> None:
        actions: list[Entitlement] = []
        hosts: list[str] = []
        for o in observed:
            if o.outcome != "allowed":
                continue
            if o.note == "unknown_host":
                if o.host not in hosts:
                    hosts.append(o.host)
                continue
            if o.name is None or o.resource is None:
                continue
            actions.append(Entitlement(o.name, _template(o.resource, arguments), "agent" if o.as_agent else "user", o.effect))
        if not actions and not hosts:
            return
        with self._lock:
            current = Entitlements.load(self.path)
            existing = current.for_tool(tool) or ToolEntitlements(tool)
            merged = _merge(existing, actions, hosts)
            doc = current.to_mapping()
            doc["tools"][tool] = merged.to_dict()
            doc["tools"] = dict(sorted(doc["tools"].items()))
            _write_atomic(self.path, doc)


def _merge(existing: ToolEntitlements, actions: Sequence[Entitlement], hosts: Sequence[str]) -> ToolEntitlements:
    out = list(existing.actions)
    for a in actions:
        for i, e in enumerate(out):
            if e.action == a.action and e.resource == a.resource and e.as_ == a.as_:
                if stronger(e.effect, a.effect) != e.effect:
                    out[i] = dataclasses.replace(e, effect=a.effect)
                break
        else:
            out.append(a)
    all_hosts = list(existing.hosts)
    for h in hosts:
        if h not in all_hosts:
            all_hosts.append(h)
    return ToolEntitlements(existing.tool, tuple(out), tuple(all_hosts))


def _write_atomic(path: str, doc: Mapping[str, Any]) -> None:
    directory = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(prefix=".toolpass-", suffix=".yaml", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("# Written by toolpass: the actions each tool performed when recorded.\n")
            yaml.safe_dump(dict(doc), f, sort_keys=False, default_flow_style=False)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

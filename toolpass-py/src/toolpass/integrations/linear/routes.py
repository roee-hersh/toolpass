"""Linear GraphQL requests, mapped to the linear integration's actions.

Linear has one endpoint; the operation is in the body. A request is routed
when it holds exactly one operation:

- a query is a read with no user permission check (Linear filters it by the
  agent's own access), and its result is untrusted;
- a mutation with exactly one top-level field is routed when toolpass knows
  that field: ``issueCreate`` checks ``team.member`` on the team in
  ``input.teamId``; ``issueUpdate``, ``issueDelete``, ``commentCreate`` and
  the other issue mutations check ``issue.edit`` on the issue they name.

The team or issue is read from the field's arguments, written inline
(``id: "ENG-1"``) or passed as variables (``input: $input`` with
``variables.input.teamId``). Any other mutation, a subscription, several
operations or fields, and a document toolpass cannot read are unresolved,
so the request is refused.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from toolpass.core.routes import Request, Route, RouteTable

__all__ = ["MUTATIONS", "ROUTES", "read_operation"]

# mutation field -> (action, resource type, where the id is: an argument, or argument.field)
MUTATIONS: dict[str, tuple[str, str, str]] = {
    "issueCreate": ("team.member", "team", "input.teamId"),
    "issueUpdate": ("issue.edit", "issue", "id"),
    "issueDelete": ("issue.edit", "issue", "id"),
    "issueArchive": ("issue.edit", "issue", "id"),
    "issueUnarchive": ("issue.edit", "issue", "id"),
    "issueAddLabel": ("issue.edit", "issue", "id"),
    "issueRemoveLabel": ("issue.edit", "issue", "id"),
    "commentCreate": ("issue.edit", "issue", "input.issueId"),
    "attachmentCreate": ("issue.edit", "issue", "input.issueId"),
    "issueRelationCreate": ("issue.edit", "issue", "input.issueId"),
}

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")  # a key (ENG, ENG-12) or a UUID


class _Unreadable(ValueError):
    pass


def _tokens(text: str) -> list[tuple[str, str]]:
    """(kind, value) tokens: n(ame), s(tring), p(unctuator), v(alue: numbers)."""
    out: list[tuple[str, str]] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c in " \t\r\n,﻿":
            i += 1
        elif c == "#":
            while i < n and text[i] not in "\r\n":
                i += 1
        elif text.startswith('"""', i):
            # A block string ends at the first """ not escaped as \""", as the server reads it.
            j, parts = i + 3, []
            while True:
                if j >= n:
                    raise _Unreadable("an unterminated block string")
                if text.startswith('\\"""', j):
                    parts.append('"""')
                    j += 4
                elif text.startswith('"""', j):
                    break
                else:
                    parts.append(text[j])
                    j += 1
            out.append(("s", "".join(parts)))
            i = j + 3
        elif c == '"':
            j, buf = i + 1, []
            while j < n and text[j] != '"':
                if text[j] == "\\" and j + 1 < n:
                    esc = text[j + 1]
                    if esc == "u":
                        digits = text[j + 2 : j + 6]
                        if len(digits) != 4 or any(d not in "0123456789abcdefABCDEF" for d in digits):
                            raise _Unreadable("a bad \\u escape")
                        buf.append(chr(int(digits, 16)))
                        j += 6
                        continue
                    buf.append({"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f"}.get(esc, esc))
                    j += 2
                    continue
                if text[j] in "\r\n":
                    raise _Unreadable("a line break inside a string")
                buf.append(text[j])
                j += 1
            if j >= n:
                raise _Unreadable("an unterminated string")
            out.append(("s", "".join(buf)))
            i = j + 1
        elif c in "{}()[]:$!=@|&":
            out.append(("p", c))
            i += 1
        elif text.startswith("...", i):
            out.append(("p", "..."))
            i += 3
        elif c.isalpha() or c == "_":
            j = i
            while j < n and (text[j].isalnum() or text[j] == "_"):
                j += 1
            out.append(("n", text[i:j]))
            i = j
        elif c.isdigit() or c == "-":
            j = i + 1
            while j < n and (text[j].isalnum() or text[j] in ".+-"):
                j += 1
            out.append(("v", text[i:j]))
            i = j
        else:
            raise _Unreadable(f"an unexpected character {c!r}")
    return out


class _Reader:
    def __init__(self, toks: list[tuple[str, str]]) -> None:
        self.t, self.i = toks, 0

    def peek(self, k: int = 0) -> tuple[str, str]:
        return self.t[self.i + k] if self.i + k < len(self.t) else ("", "")

    def take(self, kind: str = "", value: str = "") -> tuple[str, str]:
        tok = self.peek()
        if not tok[0] or (kind and tok[0] != kind) or (value and tok[1] != value):
            raise _Unreadable(f"expected {value or kind}, found {tok[1] or 'the end'}")
        self.i += 1
        return tok

    def skip_group(self, open_: str, close: str) -> None:
        depth = 0
        while True:
            kind, value = self.take()
            if kind == "p" and value == open_:
                depth += 1
            elif kind == "p" and value == close:
                depth -= 1
                if depth == 0:
                    return

    def value(self) -> Any:
        kind, v = self.peek()
        if kind == "p" and v == "$":
            self.take()
            return ("$", self.take("n")[1])
        if kind == "s":
            self.take()
            return v
        if kind == "p" and v == "{":
            self.take()
            obj: dict[str, Any] = {}
            while self.peek() != ("p", "}"):
                name = self.take("n")[1]
                self.take("p", ":")
                obj[name] = self.value()
            self.take("p", "}")
            return obj
        if kind == "p" and v == "[":
            self.take()
            items = []
            while self.peek() != ("p", "]"):
                items.append(self.value())
            self.take("p", "]")
            return items
        if kind in ("n", "v"):
            self.take()
            return ("literal", v)
        raise _Unreadable(f"an unexpected {v or 'end'} in a value")

    def arguments(self) -> dict[str, Any]:
        args: dict[str, Any] = {}
        if self.peek() == ("p", "("):
            self.take()
            while self.peek() != ("p", ")"):
                name = self.take("n")[1]
                self.take("p", ":")
                args[name] = self.value()
            self.take("p", ")")
        return args

    def directives(self) -> None:
        while self.peek() == ("p", "@"):
            self.take()
            self.take("n")
            self.arguments()


def read_operation(query: str) -> tuple[str, list[tuple[str, dict[str, Any]]]]:
    """The single operation in ``query``: its kind (query, mutation,
    subscription) and its top-level fields with their arguments. Raises
    ValueError for anything else: several operations, fragments, a document
    toolpass cannot read."""
    r = _Reader(_tokens(query))
    kind = "query"
    if r.peek()[0] == "n" and r.peek()[1] in ("query", "mutation", "subscription"):
        kind = r.take()[1]
        if r.peek()[0] == "n":
            r.take()
        if r.peek() == ("p", "("):
            r.skip_group("(", ")")
        r.directives()
    r.take("p", "{")
    fields: list[tuple[str, dict[str, Any]]] = []
    while r.peek() != ("p", "}"):
        if r.peek() == ("p", "..."):
            raise _Unreadable("a fragment spread at the top level")
        name = r.take("n")[1]
        if r.peek() == ("p", ":"):  # an alias
            r.take()
            name = r.take("n")[1]
        args = r.arguments()
        r.directives()
        if r.peek() == ("p", "{"):
            r.skip_group("{", "}")
        fields.append((name, args))
    r.take("p", "}")
    if r.peek()[0]:
        raise _Unreadable("more than one operation, or a fragment, in the document")
    return kind, fields


def _resolve(value: Any, variables: Mapping[str, Any]) -> Any:
    if isinstance(value, tuple) and value[0] == "$":
        return variables.get(value[1])
    return value


def _graphql(r: Request, vars_: Mapping[str, str]) -> dict[str, str]:
    doc = r.json()
    if not isinstance(doc, Mapping):
        return {"_unresolved": "the GraphQL request is not a JSON object"}
    query, variables = doc.get("query"), doc.get("variables") or {}
    if not isinstance(query, str) or not query.strip() or not isinstance(variables, Mapping):
        return {"_unresolved": "the GraphQL request has no query"}
    try:
        kind, fields = read_operation(query)
    except ValueError as e:
        return {"_unresolved": f"the GraphQL document cannot be read ({e}); send one operation per request"}
    if kind == "query":
        return {"_action": "", "_effect": "read", "_resource": ""}
    if kind != "mutation" or len(fields) != 1:
        return {"_unresolved": "a GraphQL request is routed when it holds one query, or a mutation with one field"}
    name, args = fields[0]
    known = MUTATIONS.get(name)
    if known is None:
        return {"_unresolved": f"the Linear mutation {name} is not routed"}
    action, rtype, where = known
    arg, _, member = where.partition(".")
    value = _resolve(args.get(arg), variables)
    if member:
        value = _resolve(value.get(member), variables) if isinstance(value, Mapping) else None
    if not isinstance(value, str) or not _ID.fullmatch(value):
        return {"_unresolved": f"{name} does not name its {rtype} in {where}"}
    return {"_action": action, "type": rtype, "id": value}


ROUTES = RouteTable(
    "linear",
    [
        Route("POST", "/", None, "{type}:{id}", effect="write", body=_graphql, spec=False, description="one GraphQL operation; queries are reads"),
    ],
)

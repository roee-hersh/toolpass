"""Snowflake SQL requests, mapped to the snowflake integration's actions.

The SQL API (``POST /api/v2/statements``) and the Python connector
(``POST /queries/v1/query-request``) carry one SQL statement in a JSON body.
toolpass reads the statement and routes it only when it is one of these
shapes, touching one table:

- ``SELECT ... FROM <table> [WHERE|GROUP BY|HAVING|QUALIFY|ORDER BY|LIMIT ...]``:
  ``table.select`` (a ``SELECT`` with no ``FROM`` reads nothing and needs no check);
- ``INSERT INTO <table> [(columns)] VALUES (...), ...``: ``table.insert``;
- ``UPDATE <table> SET ... [WHERE ...]``: ``table.update``;
- ``DELETE FROM <table> [WHERE ...]``: ``table.delete``;
- ``TRUNCATE [TABLE] [IF EXISTS] <table>``: ``table.truncate``.

The resource is ``table:DB.SCHEMA.NAME`` in Snowflake's resolved form. On
the SQL API an unqualified name takes the body's ``database`` and
``schema``; the connector's session defaults are not in its body, so there
the name must be fully qualified. toolpass assumes the account's default
SEARCH_PATH (``$current, $public``): an unqualified name that is not in the
current schema then resolves to PUBLIC, which toolpass checks as the
current schema's table, a table that does not exist and is denied.

Anything toolpass cannot read the way Snowflake does is refused: several
statements (``;``, ``MULTI_STATEMENT_COUNT``), comments, ``$`` (variables,
dollar-quoted strings), ``@`` (stages), a backslash in a string, a quoted
name with lower-case letters (QUOTED_IDENTIFIERS_IGNORE_CASE would change
its meaning), a subquery, a join or a second ``FROM``, ``IDENTIFIER()`` and
``TABLE()``, a function outside a list of built-ins (a user-defined
function may read other tables with its owner's rights), and session
parameters or body fields toolpass does not know. Bindings only fill
values: no name can be bound once ``IDENTIFIER()`` is refused.

A statement's results (``GET /api/v2/statements/{handle}``) and its
cancellation are not checked again: the handle comes from a statement that
was. The warehouse a statement runs on is not checked.
"""

from __future__ import annotations

import json
import re
import zlib
from collections.abc import Mapping, Sequence
from typing import Any

from toolpass.core.errors import go_trim_space
from toolpass.core.routes import Request, Route, RouteTable, endpoint_of_url
from toolpass.integrations.snowflake.sql import quote

__all__ = ["ACTIONS_ROUTED", "ROUTES", "read_statement"]

# Every action a route can name.
ACTIONS_ROUTED = ("table.select", "table.insert", "table.update", "table.delete", "table.truncate")

_ACCOUNT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,254}")
# A resolved name part that needs no quotes; also the only shape the body's
# database and schema may take, where an unquoted and a case-sensitive
# reading agree.
_BARE = re.compile(r"[A-Z_][A-Z0-9_$]{0,254}")
_NUMBER = re.compile(r"(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")
_OPS2 = ("<=", ">=", "<>", "!=", "||", "::", "=>", "->")
_OPS1 = frozenset("=<>+-*/%:.,()[]?")
# The largest body toolpass inflates from gzip.
_MAX_BODY = 4 << 20

# Words refused anywhere in an expression: they bring in another object,
# another statement, or a clause toolpass does not read.
_BANNED = frozenset(
    {
        "SELECT", "FROM", "JOIN", "LATERAL", "TABLE", "UNION", "INTERSECT", "EXCEPT", "MINUS", "WITH", "RECURSIVE",
        "INTO", "IDENTIFIER", "NEXTVAL", "USING", "AT", "BEFORE", "CHANGES", "PIVOT", "UNPIVOT", "MATCH_RECOGNIZE",
        "CONNECT", "START", "SAMPLE", "TABLESAMPLE", "VALUES", "SET", "INSERT", "UPDATE", "DELETE", "MERGE",
        "TRUNCATE", "CALL", "EXECUTE", "IMMEDIATE", "BEGIN", "DECLARE", "LET", "ASOF", "OVERWRITE",
    }
)  # fmt: skip
# Words that cannot be a table name or an alias unquoted.
_RESERVED = _BANNED | frozenset(
    {
        "WHERE", "GROUP", "HAVING", "QUALIFY", "ORDER", "LIMIT", "OFFSET", "FETCH", "ON", "AS", "AND", "OR", "NOT",
        "NATURAL", "INNER", "LEFT", "RIGHT", "FULL", "CROSS", "OUTER", "WINDOW", "BY", "IF", "EXISTS", "ALL",
        "DISTINCT", "NULL", "TRUE", "FALSE", "CASE", "WHEN", "THEN", "ELSE", "END", "LIKE", "ILIKE", "RLIKE", "IN",
        "IS", "BETWEEN",
    }
)  # fmt: skip
# The clauses that may follow a SELECT's table.
_SELECT_CLAUSES = frozenset({"WHERE", "GROUP", "HAVING", "QUALIFY", "ORDER", "LIMIT", "OFFSET", "FETCH"})
# Keywords and type names that may stand before "(" without being a call.
_PAREN_WORDS = frozenset(
    {
        "IN", "OVER", "AND", "OR", "NOT", "AS", "WHEN", "THEN", "ELSE", "IS", "LIKE", "ILIKE", "RLIKE", "BETWEEN",
        "EXCLUDE", "RENAME", "ANY", "ALL", "SOME", "DISTINCT", "CASE", "BY", "WITHIN", "ROWS", "RANGE", "PRECEDING",
        "FOLLOWING", "NUMBER", "DECIMAL", "NUMERIC", "VARCHAR", "CHAR", "CHARACTER", "STRING", "TEXT", "BINARY",
        "VARBINARY", "TIME", "TIMESTAMP", "TIMESTAMP_NTZ", "TIMESTAMP_LTZ", "TIMESTAMP_TZ", "DATETIME",
    }
)  # fmt: skip
# Built-in functions that compute on their arguments and touch no object.
_FUNCTIONS = frozenset(
    {
        # Aggregates and window functions.
        "COUNT", "COUNT_IF", "SUM", "AVG", "MIN", "MAX", "MEDIAN", "MODE", "STDDEV", "STDDEV_POP", "STDDEV_SAMP",
        "VARIANCE", "VAR_POP", "VAR_SAMP", "ANY_VALUE", "LISTAGG", "ARRAY_AGG", "OBJECT_AGG", "APPROX_COUNT_DISTINCT",
        "BOOLAND_AGG", "BOOLOR_AGG", "CORR", "COVAR_POP", "COVAR_SAMP", "PERCENTILE_CONT", "PERCENTILE_DISC",
        "ROW_NUMBER", "RANK", "DENSE_RANK", "PERCENT_RANK", "CUME_DIST", "NTILE", "LAG", "LEAD", "FIRST_VALUE",
        "LAST_VALUE", "NTH_VALUE", "RATIO_TO_REPORT",
        # Conditionals and casts.
        "CAST", "TRY_CAST", "COALESCE", "NVL", "NVL2", "NULLIF", "IFNULL", "IFF", "ZEROIFNULL", "NULLIFZERO",
        "DECODE", "GREATEST", "LEAST", "EQUAL_NULL",
        # Numbers.
        "ABS", "CEIL", "FLOOR", "ROUND", "TRUNC", "MOD", "POWER", "POW", "SQRT", "EXP", "LN", "LOG", "SIGN", "DIV0",
        "DIV0NULL",
        # Strings.
        "LOWER", "UPPER", "INITCAP", "LENGTH", "LEN", "CHAR_LENGTH", "CHARACTER_LENGTH", "SUBSTR", "SUBSTRING",
        "LEFT", "RIGHT", "TRIM", "LTRIM", "RTRIM", "LPAD", "RPAD", "REPLACE", "REVERSE", "CONCAT", "CONCAT_WS",
        "SPLIT", "SPLIT_PART", "CHARINDEX", "CONTAINS", "STARTSWITH", "ENDSWITH", "REGEXP_LIKE", "REGEXP_SUBSTR",
        "REGEXP_REPLACE", "REGEXP_COUNT", "REGEXP_INSTR", "TRANSLATE", "REPEAT", "ASCII", "CHR", "MD5", "SHA1",
        "SHA2", "HASH", "BASE64_ENCODE", "BASE64_DECODE_STRING", "HEX_ENCODE",
        # Conversions.
        "TO_CHAR", "TO_VARCHAR", "TO_NUMBER", "TO_DECIMAL", "TO_NUMERIC", "TO_DOUBLE", "TO_BOOLEAN", "TO_DATE",
        "TO_TIME", "TO_TIMESTAMP", "TO_TIMESTAMP_NTZ", "TO_TIMESTAMP_LTZ", "TO_TIMESTAMP_TZ", "TO_VARIANT",
        "TO_ARRAY", "TO_OBJECT", "TO_JSON", "PARSE_JSON", "TRY_PARSE_JSON", "TRY_TO_NUMBER", "TRY_TO_DATE",
        "TRY_TO_TIMESTAMP", "TRY_TO_BOOLEAN", "TRY_TO_DOUBLE",
        # Dates and times.
        "CURRENT_DATE", "CURRENT_TIME", "CURRENT_TIMESTAMP", "SYSDATE", "GETDATE", "LOCALTIMESTAMP", "DATE_TRUNC",
        "DATE_PART", "DATEADD", "DATEDIFF", "TIMEADD", "TIMEDIFF", "TIMESTAMPADD", "TIMESTAMPDIFF", "DATE_FROM_PARTS",
        "YEAR", "MONTH", "DAY", "DAYOFWEEK", "DAYOFYEAR", "WEEK", "QUARTER", "HOUR", "MINUTE", "SECOND", "LAST_DAY",
        "NEXT_DAY", "MONTHNAME", "DAYNAME", "CONVERT_TIMEZONE", "DATE",
        # Semi-structured values.
        "ARRAY_CONSTRUCT", "ARRAY_SIZE", "ARRAY_CONTAINS", "ARRAY_TO_STRING", "OBJECT_CONSTRUCT", "GET", "GET_PATH",
        "IS_NULL_VALUE", "TYPEOF", "UUID_STRING",
    }
)  # fmt: skip

# Session parameters a request may set: output formats and tags, which do not change how a statement reads.
_PARAMETERS = frozenset(
    {
        "timezone", "query_tag", "binary_output_format", "date_output_format", "time_output_format",
        "timestamp_output_format", "timestamp_ltz_output_format", "timestamp_ntz_output_format",
        "timestamp_tz_output_format", "multi_statement_count", "rows_per_resultset", "use_cached_result",
    }
)  # fmt: skip
_SQLAPI_FIELDS = frozenset({"statement", "timeout", "database", "schema", "warehouse", "role", "bindings", "parameters", "resultSetMetaData"})
_CONNECTOR_FIELDS = frozenset(
    {"sqlText", "asyncExec", "sequenceId", "querySubmissionTime", "isInternal", "describeOnly", "parameters", "bindings", "queryContextDTO", "describedJobId"}
)


class _Unreadable(ValueError):
    pass


_Token = tuple[str, str]  # (kind, value): w(ord, upper-cased), q(uoted name, resolved), s(tring), n(umber), p(unctuator)


def _quoted(sql: str, i: int) -> tuple[str, int]:
    """The quoted name starting at i, resolved, and the index after it."""
    j, buf = i + 1, []
    while True:
        if j >= len(sql):
            raise _Unreadable("an unterminated quoted name")
        if sql[j] == '"':
            if sql.startswith('""', j):
                buf.append('"')
                j += 2
                continue
            break
        buf.append(sql[j])
        j += 1
    name = "".join(buf)
    if not name or len(name) > 255:
        raise _Unreadable("a quoted name is empty or longer than 255 characters")
    if any(not " " <= c <= "~" or c == "?" for c in name):
        raise _Unreadable("a quoted name holds a character other than printable ASCII")
    if any("a" <= c <= "z" for c in name):
        raise _Unreadable(f'the quoted name "{name}" has lower-case letters, which QUOTED_IDENTIFIERS_IGNORE_CASE would read otherwise')
    return name, j + 1


def _tokens(sql: str) -> list[_Token]:
    out: list[_Token] = []
    i, n = 0, len(sql)
    while i < n:
        c = sql[i]
        if c in " \t\r\n":
            i += 1
        elif sql.startswith(("--", "//", "/*"), i):
            raise _Unreadable("a comment")
        elif c == "'":
            j = i + 1
            while True:
                if j >= n:
                    raise _Unreadable("an unterminated string")
                if sql[j] == "\\":
                    raise _Unreadable("a backslash in a string")
                if sql[j] == "'":
                    if sql.startswith("''", j):
                        j += 2
                        continue
                    break
                j += 1
            out.append(("s", ""))
            i = j + 1
        elif c == '"':
            name, i = _quoted(sql, i)
            out.append(("q", name))
        elif c.isascii() and (c.isalpha() or c == "_"):
            j = i + 1
            while j < n and sql[j].isascii() and (sql[j].isalnum() or sql[j] in "_$"):
                j += 1
            out.append(("w", sql[i:j].upper()))
            i = j
        elif (c.isascii() and c.isdigit()) or (c == "." and i + 1 < n and sql[i + 1].isascii() and sql[i + 1].isdigit()):
            m = _NUMBER.match(sql, i)
            assert m is not None
            j = m.end()
            if j < n and (sql[j].isalnum() or sql[j] in "_$"):
                raise _Unreadable("a number runs into a name")
            out.append(("n", sql[i:j]))
            i = j
        elif sql[i : i + 2] in _OPS2:
            if sql[i : i + 2] == "->":
                raise _Unreadable("-> (a lambda or the ->> flow operator)")
            out.append(("p", sql[i : i + 2]))
            i += 2
        elif c in _OPS1:
            out.append(("p", c))
            i += 1
        elif c == ";":
            raise _Unreadable("a ; (more than one statement)")
        else:
            raise _Unreadable(f"the character {c!r}")
    return out


def _check_expr(toks: Sequence[_Token]) -> None:
    """An expression region: no word that brings in another object, calls
    only to known built-ins, balanced parentheses."""
    depth = 0
    for k, (kind, v) in enumerate(toks):
        nxt = toks[k + 1] if k + 1 < len(toks) else ("", "")
        if kind == "w":
            if v in _BANNED:
                raise _Unreadable(f"{v} inside the statement (a subquery, a join, a second statement or a clause toolpass does not read)")
            if nxt == ("p", "("):
                if k and toks[k - 1] == ("p", "."):
                    raise _Unreadable(f"a qualified function call (.{v})")
                if v not in _FUNCTIONS and v not in _PAREN_WORDS:
                    raise _Unreadable(f"the function {v} is not one toolpass knows touches no other object")
        elif kind == "q" and nxt == ("p", "("):
            raise _Unreadable("a quoted function name")
        elif (kind, v) == ("p", "("):
            depth += 1
        elif (kind, v) == ("p", ")"):
            depth -= 1
            if depth < 0:
                raise _Unreadable("unbalanced parentheses")
    if depth:
        raise _Unreadable("unbalanced parentheses")


def _at(toks: Sequence[_Token], i: int) -> _Token:
    return toks[i] if i < len(toks) else ("", "")


def _name(toks: Sequence[_Token], i: int) -> tuple[list[str], int]:
    """A table name of one to three dotted parts, resolved."""
    parts: list[str] = []
    while True:
        kind, v = _at(toks, i)
        if kind == "q" or (kind == "w" and v not in _RESERVED and _BARE.fullmatch(v)):
            parts.append(v)
        else:
            raise _Unreadable(f"{v or 'the end'} where a table name was expected")
        i += 1
        if _at(toks, i) != ("p", ".") or len(parts) == 3:
            return parts, i
        i += 1


def _alias(toks: Sequence[_Token], i: int) -> int:
    kind, v = _at(toks, i)
    if (kind, v) == ("w", "AS"):
        kind, v = _at(toks, i + 1)
        if not (kind == "q" or (kind == "w" and v not in _RESERVED)):
            raise _Unreadable("AS without an alias")
        return i + 2
    if kind == "q" or (kind == "w" and v not in _RESERVED):
        return i + 1
    return i


def _depth0(toks: Sequence[_Token], word: str) -> int:
    """The index of the first ``word`` outside parentheses, or -1."""
    depth = 0
    for k, t in enumerate(toks):
        if t == ("p", "("):
            depth += 1
        elif t == ("p", ")"):
            depth -= 1
        elif depth == 0 and t == ("w", word):
            return k
    return -1


def _values(toks: Sequence[_Token]) -> None:
    """``(...), (...)``: only row groups at the top level."""
    if not toks:
        raise _Unreadable("VALUES without rows")
    depth, expect_group = 0, True
    for t in toks:
        if depth == 0:
            if expect_group and t == ("p", "("):
                expect_group = False
            elif not expect_group and t == ("p", ","):
                expect_group = True
                continue
            else:
                raise _Unreadable("VALUES may hold only rows of values")
        if t == ("p", "("):
            depth += 1
        elif t == ("p", ")"):
            depth -= 1
    if expect_group or depth:
        raise _Unreadable("VALUES may hold only rows of values")
    _check_expr(toks)


def _columns(toks: Sequence[_Token], i: int) -> int:
    """An INSERT's column list ``(a, b)``, when there is one."""
    if _at(toks, i) != ("p", "("):
        return i
    i += 1
    while True:
        kind, v = _at(toks, i)
        if not (kind == "q" or (kind == "w" and v not in _RESERVED)):
            raise _Unreadable("the column list may hold only column names")
        i += 1
        if _at(toks, i) == ("p", ")"):
            return i + 1
        if _at(toks, i) != ("p", ","):
            raise _Unreadable("the column list may hold only column names")
        i += 1


def _parse(toks: Sequence[_Token]) -> tuple[str, list[str] | None]:
    """The action and the table name of a statement, or None for a SELECT that reads no table."""
    first = _at(toks, 0)
    if first == ("w", "SELECT"):
        f = _depth0(toks, "FROM")
        if f < 0:
            if len(toks) < 2:
                raise _Unreadable("SELECT without anything to select")
            _check_expr(toks[1:])
            return "table.select", None
        if f < 2:
            raise _Unreadable("SELECT without anything to select")
        _check_expr(toks[1:f])
        parts, j = _name(toks, f + 1)
        j = _alias(toks, j)
        rest = toks[j:]
        if rest and not (rest[0][0] == "w" and rest[0][1] in _SELECT_CLAUSES):
            raise _Unreadable(f"{rest[0][1]} after the table (a join, a second table, or a clause toolpass does not read)")
        _check_expr(rest)
        return "table.select", parts
    if first == ("w", "INSERT"):
        if _at(toks, 1) != ("w", "INTO"):
            raise _Unreadable("only INSERT INTO is read (not OVERWRITE, ALL or FIRST)")
        parts, j = _name(toks, 2)
        j = _columns(toks, j)
        if _at(toks, j) != ("w", "VALUES"):
            raise _Unreadable("only INSERT ... VALUES is read")
        _values(toks[j + 1 :])
        return "table.insert", parts
    if first == ("w", "UPDATE"):
        parts, j = _name(toks, 1)
        j = _alias(toks, j)
        if _at(toks, j) != ("w", "SET") or j + 1 >= len(toks):
            raise _Unreadable("UPDATE without SET")
        _check_expr(toks[j + 1 :])
        return "table.update", parts
    if first == ("w", "DELETE"):
        if _at(toks, 1) != ("w", "FROM"):
            raise _Unreadable("DELETE without FROM")
        parts, j = _name(toks, 2)
        j = _alias(toks, j)
        rest = toks[j:]
        if rest and rest[0] != ("w", "WHERE"):
            raise _Unreadable(f"{rest[0][1]} after the table (USING, or a clause toolpass does not read)")
        _check_expr(rest)
        return "table.delete", parts
    if first == ("w", "TRUNCATE"):
        j = 1
        if _at(toks, j) == ("w", "TABLE"):
            j += 1
        if _at(toks, j) == ("w", "IF") and _at(toks, j + 1) == ("w", "EXISTS"):
            j += 2
        parts, j = _name(toks, j)
        if j != len(toks):
            raise _Unreadable("TRUNCATE names one table and nothing else")
        return "table.truncate", parts
    raise _Unreadable(f"{first[1] or 'an empty statement'} is not a statement toolpass reads (SELECT, INSERT, UPDATE, DELETE, TRUNCATE)")


def _render(parts: Sequence[str]) -> str:
    return ".".join(p if _BARE.fullmatch(p) else quote(p) for p in parts)


def read_statement(sql: str, database: str | None = None, schema: str | None = None) -> tuple[str | None, str, str]:
    """(action, resource, effect) for one statement, with the session's
    database and schema for an unqualified name (None when unknown).
    Raises ValueError for a statement toolpass does not read exactly."""
    toks = _tokens(sql)
    action, parts = _parse(toks)
    effect = {"table.select": "read", "table.insert": "write", "table.update": "write"}.get(action, "destructive")
    if parts is None:
        return None, "", effect
    if len(parts) < 3:
        need = [database] if len(parts) == 2 else [database, schema]
        if any(p is None for p in need):
            where = "the request gives no database" if len(parts) == 2 else "the request does not give both database and schema"
            raise _Unreadable(f"the table {_render(parts)} is not fully qualified and {where}")
        parts = [p for p in need if p is not None] + parts
    return action, "table:" + _render(parts), effect


# -- request bodies ---------------------------------------------------------------


def _pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
    keys = [k for k, _ in items]
    if len(set(keys)) != len(keys):
        raise _Unreadable("a JSON member is given twice")
    return dict(items)


def _reject_constant(name: str) -> Any:
    raise _Unreadable(f"{name} is not JSON")


def _json_object(body: bytes) -> dict[str, Any]:
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        raise _Unreadable("the body is not UTF-8") from None
    try:
        doc = json.loads(text, object_pairs_hook=_pairs, parse_constant=_reject_constant)
    except _Unreadable:
        raise
    except ValueError:
        raise _Unreadable("the body is not JSON") from None
    if not isinstance(doc, dict):
        raise _Unreadable("the body is not a JSON object")
    return doc


def _parameters(v: Any) -> None:
    """Session parameters: only known ones, and one statement."""
    if v is None:
        return
    if not isinstance(v, dict):
        raise _Unreadable("parameters is not an object")
    seen: set[str] = set()
    for k, x in v.items():
        key = k.lower()
        if key in seen:
            raise _Unreadable(f"the parameter {k} is given twice")
        seen.add(key)
        if key not in _PARAMETERS:
            raise _Unreadable(f"the session parameter {k} is not one toolpass knows leaves the statement's meaning alone")
        if key == "multi_statement_count" and (isinstance(x, bool) or x != 1):
            raise _Unreadable("MULTI_STATEMENT_COUNT other than 1 (more than one statement)")


def _default(doc: Mapping[str, Any], key: str) -> str | None:
    v = doc.get(key)
    if v is None:
        return None
    if not isinstance(v, str) or not _BARE.fullmatch(v):
        raise _Unreadable(f"{key} must be an upper-case name such as SALES, where Snowflake's case-sensitive reading and an unquoted one agree")
    return v


def _observed(action: str | None, resource: str, effect: str) -> dict[str, str]:
    return {"_action": action or "", "_resource": resource, "_effect": effect}


def _sqlapi(r: Request, vars_: Mapping[str, str]) -> dict[str, str]:
    """POST /api/v2/statements."""
    if r.content_type != "application/json":
        return {"_unresolved": "a SQL API request is read only as JSON (Content-Type: application/json)"}
    try:
        doc = _json_object(r.body or b"")
        extra = sorted(set(doc) - _SQLAPI_FIELDS)
        if extra:
            raise _Unreadable(f"the body carries {', '.join(extra)}, which toolpass does not know")
        statement = doc.get("statement")
        if not isinstance(statement, str) or not statement.strip():
            raise _Unreadable("the body has no statement")
        if not isinstance(doc.get("bindings") or {}, dict):
            raise _Unreadable("bindings is not an object")
        _parameters(doc.get("parameters"))
        return _observed(*read_statement(statement, _default(doc, "database"), _default(doc, "schema")))
    except _Unreadable as e:
        return {"_unresolved": f"the SQL statement cannot be read exactly ({e}); send one statement on one table"}


def _connector(r: Request, vars_: Mapping[str, str]) -> dict[str, str]:
    """POST /queries/v1/query-request, the Python connector's own protocol;
    the body may be gzip-compressed."""
    if r.content_type != "application/json":
        return {"_unresolved": "a query request is read only as JSON (Content-Type: application/json)"}
    try:
        body = r.body or b""
        encoding = r.headers.get("content-encoding", "").strip().lower()
        if encoding == "gzip":
            d = zlib.decompressobj(16 + zlib.MAX_WBITS)
            try:
                body = d.decompress(body, _MAX_BODY)
            except zlib.error:
                raise _Unreadable("the gzip body cannot be read") from None
            if d.unconsumed_tail or not d.eof or d.unused_data:
                raise _Unreadable("the gzip body is too large or is not one stream")
        elif encoding not in ("", "identity"):
            raise _Unreadable(f"the body's encoding {encoding} is not read")
        doc = _json_object(body)
        extra = sorted(k for k in set(doc) - _CONNECTOR_FIELDS if doc[k] is not None)
        if extra:
            raise _Unreadable(f"the body carries {', '.join(extra)}, which toolpass does not read")
        sql = doc.get("sqlText")
        if not isinstance(sql, str) or not sql.strip():
            raise _Unreadable("the body has no sqlText")
        if not isinstance(doc.get("bindings") or {}, dict):
            raise _Unreadable("bindings is not an object")
        _parameters(doc.get("parameters"))
        # The session's database and schema are set at login, not in this body.
        return _observed(*read_statement(sql))
    except _Unreadable as e:
        return {"_unresolved": f"the SQL statement cannot be read exactly ({e}); send one statement on one fully qualified table"}


def _locate(settings: Any) -> Sequence[tuple[str, str]]:
    """The account URL, by default https://<account>.snowflakecomputing.com
    (the account with its underscores as hyphens, as the integration builds
    it, and as given, which the connector uses)."""
    get = getattr(settings, "get", None)
    if not callable(get):
        return []
    url = go_trim_space(get("url") or "").rstrip("/")
    if url:
        ep = endpoint_of_url(url)
        return [ep] if ep is not None else []
    account = go_trim_space(get("account") or "").lower()
    if not _ACCOUNT_RE.fullmatch(account):
        return []
    hosts = dict.fromkeys((account.replace("_", "-"), account))
    return [(h + ".snowflakecomputing.com", "") for h in hosts]


def _routes() -> list[Route]:
    own = "the agent's own session with Snowflake"
    statement = "one SQL statement on one table; the action is the statement's (table.select, table.insert, ...)"
    return [
        Route("POST", "/api/v2/statements", None, "", body=_sqlapi, description=statement),
        Route(
            "GET",
            "/api/v2/statements/{statementHandle}",
            None,
            "",
            description="a statement's status and results; the handle comes from a statement toolpass checked",
        ),
        Route("POST", "/api/v2/statements/{statementHandle}/cancel", None, "", untrusted_output=False, description="cancel a statement toolpass checked"),
        # The Python connector.
        Route("POST", "/queries/v1/query-request", None, "", body=_connector, spec=False, description=statement),
        Route("POST", "/queries/v1/abort-request", None, "", untrusted_output=False, spec=False, description="cancel a statement toolpass checked"),
        Route("POST", "/session/v1/login-request", None, "", untrusted_output=False, spec=False, description=own),
        Route("POST", "/session/token-request", None, "", untrusted_output=False, spec=False, description=own),
        Route("POST", "/session/heartbeat", None, "", untrusted_output=False, spec=False, description=own),
        Route("POST", "/session", None, "", untrusted_output=False, spec=False, description=own + " (logging out)"),
        Route("POST", "/telemetry/send", None, "", untrusted_output=False, spec=False, description="the connector's own telemetry to Snowflake"),
    ]


ROUTES = RouteTable("snowflake", _routes(), claims=("/api/v2/statements",), locate=_locate)

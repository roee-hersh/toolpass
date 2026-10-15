#!/usr/bin/env python3
"""List the operations of a fetched API description under a path prefix, to
hand-classify them into an integration's route table.

    test/specs/routes_draft.py /tmp/specs/github.spec '/repos/{owner}/{repo}' [--writes]

Prints one line per operation: METHOD, path template, operationId and the
summary. With --writes, GET/HEAD/OPTIONS are left out. Reads OpenAPI 3 and
Swagger 2 descriptions (JSON or YAML); variable names in the prefix do not
matter, only the shape.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

METHODS = ("get", "put", "post", "delete", "patch", "head", "options")


def shape(path: str) -> str:
    return re.sub(r"\{[^}]*\}", "{}", path)


def load(path: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    try:
        doc = json.loads(raw)
    except ValueError:
        import yaml

        doc = yaml.safe_load(raw)
    if not isinstance(doc, dict):
        raise SystemExit(f"{path}: not an API description")
    return doc


def main(argv: list[str]) -> int:
    args = [a for a in argv if not a.startswith("--")]
    if len(args) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    writes_only = "--writes" in argv
    doc = load(Path(args[0]))
    prefix = shape(args[1].rstrip("/"))
    rows: list[tuple[str, str, str, str]] = []
    for p, item in sorted(doc.get("paths", {}).items()):
        s = shape(p)
        if not (s == prefix or s.startswith(prefix + "/")):
            continue
        if not isinstance(item, dict):
            continue
        for method, op in item.items():
            if method not in METHODS or not isinstance(op, dict):
                continue
            if writes_only and method in ("get", "head", "options"):
                continue
            rows.append((method.upper(), p, str(op.get("operationId", "")), str(op.get("summary", "")).strip()))
    for method, p, op_id, summary in rows:
        print(f"{method:7} {p}  [{op_id}] {summary}")
    print(f"# {len(rows)} operations", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

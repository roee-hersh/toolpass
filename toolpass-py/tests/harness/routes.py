"""Hold an integration's route table to the vendor's API description.

Two checks, run when $TOOLPASS_SPECS_DIR has the description:

- every route that claims to describe one operation (``spec=True``) names a
  method and path the description has;
- every write operation of the description under the table's ``claims`` is
  routed or listed in ``unrouted`` with a reason, so a gap is a decision,
  not an accident.
"""

from __future__ import annotations

import re
from typing import Any

from tests.harness.spec import spec_from_env
from toolpass.core.routes import PathTemplate, Request, RouteTable

WRITES = ("POST", "PUT", "PATCH", "DELETE")


def sample_path(template: str) -> str:
    """A concrete path for a template: each variable becomes its own name."""
    return re.sub(r"\{\+?([A-Za-z0-9_]+)\}", lambda m: m.group(1), template)


def spec_template(path: str) -> PathTemplate:
    """A description's path as a template. A segment that mixes text and a
    variable (GitLab's ``binary-{architecture}``) counts as one variable."""
    segs = ["{" + re.sub(r"\W", "_", s) + "}" if ("{" in s or "}" in s) and not re.fullmatch(r"\{[A-Za-z0-9_]+\+?\}", s) else s for s in path.split("/")]
    return PathTemplate("/".join(segs))


def _whole_segments(path: str) -> str:
    """A description path with a variable that shares its segment with a
    suffix (Zendesk's ``/countries/{id}.json``) as that variable alone, the
    way a route template names it."""
    return re.sub(r"\{([A-Za-z0-9_-]+)\}[^/{}]+(?=/|$)", r"{\1}", path)


def spec_operations(name: str) -> list[tuple[str, str]] | None:
    """(METHOD, template) for every operation of a loaded OpenAPI/Swagger
    description, or None when the description is not at hand."""
    spec = spec_from_env(name)
    if spec is None:
        return None
    ops: dict[str, dict[str, Any]] | None = getattr(spec, "ops", None)
    if ops is None:
        raise TypeError(f"{name}: not an OpenAPI description")
    return [(method, _whole_segments(path)) for path, by_method in ops.items() for method in by_method]


def check_routes_against_spec(table: RouteTable, name: str, *, host: str) -> list[str]:
    """Every problem found, as messages; empty when the table matches."""
    ops = spec_operations(name)
    if ops is None:
        return []
    shapes = {(m, spec_template(p).shape()) for m, p in ops}
    paths = {spec_template(p).shape() for _, p in ops}
    problems: list[str] = []
    for r in table.routes:
        if not r.spec:
            continue
        shape = PathTemplate(r.path).shape()
        if r.method == "*":
            if shape not in paths:
                problems.append(f"route * {r.path}: no such path in the {name} description")
        elif (r.method.upper(), shape) not in shapes:
            problems.append(f"route {r.method} {r.path}: no such operation in the {name} description")
    claims = [PathTemplate(c).shape() for c in table.claims]
    for method, path in ops:
        if method not in WRITES:
            continue
        shape = spec_template(path).shape()
        if not any(shape == c or shape.startswith(c.rstrip("/") + "/") for c in claims):
            continue
        concrete = sample_path(path)
        req = Request("https", host, 443, method, concrete, "", {"content-type": "application/json"}, b"{}")
        if table.match(req) is None and table.is_unrouted(method, concrete) is None:
            problems.append(f"{method} {path} is not routed and not listed as unrouted")
    return problems

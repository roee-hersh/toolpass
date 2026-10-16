"""GitLab REST requests (``/api/v4``), mapped to the gitlab integration's
actions on the project or group they touch.

The project or group comes from the path: a numeric id or the URL-encoded
path (``acme%2Fwebapp``), checked as ``project:<id>`` or ``group:<id>``. A
request that names a branch (a commit, a file, a new or deleted branch)
checks ``project:<id>@<branch>`` so protected-branch rules apply; one that
pushes without naming its branch checks the project, which the integration
answers unknown when the project has protected branches.

Parameters are read as GitLab reads them: the query string merged with a
JSON or URL-encoded body. A body of another type, a name given twice or
both in the query and the body with different values, or a nested form key
(``branch[]``) are refused where the route reads that name.

Refused, or left out on purpose: writes that reach a second project or group
named in the body (moving or cloning an issue, a merge request into another
project, forking into a group, sharing with a group, importing members);
writes that only an Owner or an administrator may do and the integration has
no action for (deleting an issue or a merge request, archiving, transfers);
tags, whose protection rules the integration does not evaluate; and
changes to a member who may be an Owner. Writes GitLab allows a lower role
than the integration has an action for (wikis, snippets, environments) check
``project.admin``, the stricter answer.
"""

from __future__ import annotations

import json
import urllib.parse
from collections.abc import Callable, Mapping
from typing import Any

from toolpass.core.routes import Extractor, Request, Route, RouteTable, endpoint_of_url
from toolpass.integrations.gitlab.actions import LEVEL_OWNER, validate_branch, validate_path

__all__ = ["ROUTES", "read_params"]

_A = "/api/v4"
_P = _A + "/projects/{id}"
_G = _A + "/groups/{id}"
_PROJECT = "project:{id}"
_PROJECT_AT = "project:{id}{at}"
_GROUP = "group:{id}"

_RD, _IC, _IE, _MC, _MA = "project.read", "issue.create", "issue.edit", "mr.create", "mr.approve"
_PUSH, _MERGE, _BP, _ADM, _MEM = "repo.push", "mr.merge", "branch.protect", "project.admin", "member.manage"
_DEL, _RUN, _VAR, _RUNNER = "project.delete", "pipeline.run", "variable.manage", "runner.manage"
_GM, _GA, _GPC = "group.member", "group.admin", "group.project.create"

_FALSE = (False, 0, "false", "0", "")
_SELF_ROTATE = "rotates the agent's own token"


# -- parameters ------------------------------------------------------------------


class Params:
    """A request's parameters, as GitLab's API reads them: the query string,
    then the body (JSON object or URL-encoded form). ``ambiguous`` holds the
    names given more than once, or in the query and the body with different
    values, or as nested form keys; ``error`` is set when the parameters
    cannot be read at all."""

    def __init__(self, values: dict[str, Any], ambiguous: set[str], error: str = "") -> None:
        self.values = values
        self.ambiguous = ambiguous
        self.error = error

    def get(self, name: str) -> tuple[Any, str]:
        """(value or None, reason it cannot be used or "")."""
        if self.error:
            return None, self.error
        if name in self.ambiguous:
            return None, f"{name} is given more than once; give it once"
        return self.values.get(name), ""

    def string(self, name: str) -> tuple[str | None, str]:
        """A string parameter: None when absent; a reason when it is not a string."""
        v, why = self.get(name)
        if why:
            return None, why
        if v is None:
            return None, ""
        if isinstance(v, bool) or not isinstance(v, (str, int)):
            return None, f"{name} is not a string"
        return str(v), ""


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in pairs:
        if k in out:
            raise ValueError(f"the JSON body gives {k} more than once")
        out[k] = v
    return out


def _form_pairs(text: str) -> list[tuple[str, str]]:
    if not text:
        return []
    if ";" in text:
        # Some servers split on ";" as well as "&": refuse rather than guess.
        raise ValueError("the parameters contain a raw ';'")
    return urllib.parse.parse_qsl(text, keep_blank_values=True, strict_parsing=True, errors="strict")


def read_params(r: Request) -> Params:
    """The request's parameters, or a Params whose ``error`` says why they cannot be read exactly."""
    try:
        query = _form_pairs(r.query)
    except (ValueError, UnicodeDecodeError):
        return Params({}, set(), "the query string cannot be read exactly")
    body: list[tuple[str, Any]] = []
    if r.body:
        ct = r.content_type
        try:
            text = r.body.decode("utf-8")
        except UnicodeDecodeError:
            return Params({}, set(), "the body is not UTF-8")
        if ct == "application/json":
            try:
                doc = json.loads(text, object_pairs_hook=_unique_pairs)
            except ValueError:
                return Params({}, set(), "the JSON body cannot be read, or gives a name more than once")
            if not isinstance(doc, dict):
                return Params({}, set(), "the JSON body is not an object")
            body = list(doc.items())
        elif ct == "application/x-www-form-urlencoded":
            try:
                body = list(_form_pairs(text))
            except (ValueError, UnicodeDecodeError):
                return Params({}, set(), "the form body cannot be read exactly")
        else:
            return Params({}, set(), f"a body of type {ct or 'unknown'!r} is not read; send JSON or a URL-encoded form")
    values: dict[str, Any] = {}
    ambiguous: set[str] = set()
    seen_query: set[str] = set()
    for k, v in query:
        base = k.split("[", 1)[0]
        if base != k or base in seen_query:
            ambiguous.add(base)
        seen_query.add(base)
        values[base] = v
    seen_body: set[str] = set()
    for k, v in body:
        base = k.split("[", 1)[0]
        if base != k or base in seen_body:
            ambiguous.add(base)
        seen_body.add(base)
        if base in values and str(values[base]) != str(v):
            ambiguous.add(base)
        values[base] = v
    return Params(values, ambiguous)


# -- extractors --------------------------------------------------------------------


def _chain(*extractors: Extractor) -> Extractor:
    """Run extractors in order; the first refusal wins."""

    def extract(r: Request, vars_: Mapping[str, str]) -> dict[str, str]:
        out: dict[str, str] = {}
        merged = dict(vars_)
        for e in extractors:
            got = e(r, merged)
            if got is None:
                return {"_unresolved": "the request does not name a resource toolpass can check"}
            got = dict(got)
            if "_unresolved" in got:
                return {"_unresolved": got["_unresolved"]}
            out.update(got)
            merged.update(got)
        return out

    return extract


def _id_ok(what: str, rid: str) -> str:
    try:
        validate_path(rid)
    except ValueError as e:
        return f"the {what} {rid!r} is not a numeric id or a path such as acme/webapp ({e})"
    return ""


def _path_ids(r: Request, vars_: Mapping[str, str]) -> dict[str, str]:
    """The project or group in the path must be an id or a path, and a
    parameter of the same name must not name another one."""
    why = _id_ok("project or group", vars_.get("id", ""))
    if why:
        return {"_unresolved": why}
    p = read_params(r)
    if not p.error:
        for name, value in vars_.items():
            if name == "rest":
                continue
            v, bad = p.string(name)
            if bad or (v is not None and v != value):
                return {"_unresolved": f"the parameter {name} names something other than the path does"}
    return {}


def _at(branch: str | None) -> dict[str, str]:
    return {"at": f"@{branch}" if branch else ""}


def _branch_ok(branch: str) -> str:
    if "@" in branch:
        return f"the branch {branch!r} contains @, which toolpass cannot put in a resource"
    try:
        validate_branch(branch)
    except ValueError as e:
        return f"the branch {branch!r} is not a valid branch name ({e})"
    return ""


def _same_project(p: Params, vars_: Mapping[str, str], *names: str) -> str:
    """A parameter naming a project must name the one in the path, the same way."""
    for name in names:
        v, why = p.string(name)
        if why:
            return why
        if v is not None and v != vars_.get("id"):
            return f"{name} names another project than the path; toolpass checks one project per request"
    return ""


def _off(p: Params, *names: str) -> str:
    """A boolean parameter that must be absent or false."""
    for name in names:
        v, why = p.get(name)
        if why:
            return why
        if v is not None and v not in _FALSE:
            return f"{name} is not checked by toolpass; send the request without it"
    return ""


def _body_branch(name: str, *, required: bool = True) -> Extractor:
    """The branch a write lands on, from a parameter."""

    def extract(r: Request, vars_: Mapping[str, str]) -> dict[str, str]:
        p = read_params(r)
        branch, why = p.string(name)
        if why:
            return {"_unresolved": why}
        if not branch:
            if required:
                return {"_unresolved": f"the request does not name the branch ({name})"}
            return _at(None)
        why = _branch_ok(branch) or _same_project(p, vars_, "start_project") or _off(p, "force")
        if why:
            return {"_unresolved": why}
        return _at(branch)

    return extract


def _path_branch(r: Request, vars_: Mapping[str, str]) -> dict[str, str]:
    branch = vars_.get("branch", "")
    why = _branch_ok(branch)
    return {"_unresolved": why} if why else _at(branch)


def _mr_create(r: Request, vars_: Mapping[str, str]) -> dict[str, str]:
    """A merge request lands in target_project_id when given: it must be this project."""
    why = _same_project(read_params(r), vars_, "target_project_id")
    return {"_unresolved": why} if why else {}


def _issue_link(r: Request, vars_: Mapping[str, str]) -> dict[str, str]:
    why = _same_project(read_params(r), vars_, "target_project_id")
    return {"_unresolved": why} if why else {}


def _merge(r: Request, vars_: Mapping[str, str]) -> dict[str, str]:
    """Removing the source branch on merge is a push to a branch (maybe in a fork) the request does not name."""
    why = _off(read_params(r), "should_remove_source_branch")
    return {"_unresolved": why} if why else {}


def _new_member(r: Request, vars_: Mapping[str, str]) -> dict[str, str]:
    """Only an Owner may grant Owner, and a custom role can add abilities the level does not show."""
    p = read_params(r)
    level, why = p.string("access_level")
    if why:
        return {"_unresolved": why}
    if level is not None and (not level.isdigit() or int(level) >= LEVEL_OWNER):
        return {"_unresolved": "granting the Owner role (or an unknown level) needs an Owner, which member.manage does not check"}
    role, why = p.get("member_role_id")
    if why or role is not None:
        return {"_unresolved": why or "a custom role (member_role_id) is not checked by toolpass"}
    return {}


def _fork(r: Request, vars_: Mapping[str, str]) -> dict[str, str]:
    """A fork into a namespace named in the body is a second target; without
    one the fork lands in the agent's own namespace, and reading the project
    is what it takes."""
    p = read_params(r)
    for name in ("namespace", "namespace_id", "namespace_path"):
        v, why = p.get(name)
        if why or v is not None:
            return {"_unresolved": why or f"a fork into another namespace ({name}) needs two checks; toolpass refuses it"}
    return {}


def _new_project(r: Request, vars_: Mapping[str, str]) -> dict[str, str]:
    """POST /projects: in the group namespace_id names, or in the agent's own namespace."""
    p = read_params(r)
    for name in ("namespace", "namespace_path", "template_project_id", "use_custom_template", "group_with_project_templates_id", "import_url"):
        v, why = p.get(name)
        if why or v is not None:
            return {"_unresolved": why or f"{name} reaches another project or source; toolpass refuses it"}
    ns, why = p.string("namespace_id")
    if why:
        return {"_unresolved": why}
    if ns is None:
        return {"_resource": "", "_action": ""}
    if not ns.isdigit():
        return {"_unresolved": f"namespace_id {ns!r} is not a numeric id"}
    return {"group": ns}


def _new_group(r: Request, vars_: Mapping[str, str]) -> dict[str, str]:
    """POST /groups: a subgroup of parent_id; a top-level group is not checked."""
    parent, why = read_params(r).string("parent_id")
    if why:
        return {"_unresolved": why}
    if parent is None:
        return {"_unresolved": "a top-level group is created in the instance, which toolpass has no action for"}
    why = _id_ok("parent group", parent)
    return {"_unresolved": why} if why else {"group": parent}


def _release(r: Request, vars_: Mapping[str, str]) -> dict[str, str]:
    """A release whose tag does not exist yet creates it from ``ref``: tags are not routed."""
    v, why = read_params(r).get("ref")
    if why or v is not None:
        return {"_unresolved": why or "a release that creates its tag (ref) is a tag push, which toolpass does not route"}
    return {}


def _refuse(reason: str) -> Extractor:
    """A request toolpass refuses inside a subtree it otherwise routes."""

    def extract(r: Request, vars_: Mapping[str, str]) -> dict[str, str]:
        return {"_unresolved": reason}

    return extract


# -- routes ------------------------------------------------------------------------------

_WRITES = ("POST", "PUT", "PATCH", "DELETE")


def _p(method: str, sub: str, action: str | None, *more: Extractor, resource: str = _PROJECT, **kw: Any) -> Route:
    return Route(method, _P + sub, action, resource, body=_chain(_path_ids, *more), **kw)


def _g(method: str, sub: str, action: str | None, *more: Extractor, resource: str = _GROUP, **kw: Any) -> Route:
    return Route(method, _G + sub, action, resource, body=_chain(_path_ids, *more), **kw)


def _subtree(make: Callable[..., Route], sub: str, write: str, read: str | None = None, *, sends_out: bool = False) -> list[Route]:
    """Every request under ``sub``: writes check ``write``, reads ``read`` (``write`` when None)."""
    out = [
        make("GET", sub, read or write, spec=False),
        make("GET", sub + "/{rest+}", read or write, spec=False),
    ]
    for m in _WRITES:
        out.append(make(m, sub, write, sends_out=sends_out, spec=False))
        out.append(make(m, sub + "/{rest+}", write, sends_out=sends_out, spec=False))
    return out


def _routes() -> list[Route]:
    out: list[Route] = [
        # Reads: a project or group at any level; the sensitive parts below check more.
        _p("GET", "", _RD),
        _p("GET", "/{rest+}", _RD, spec=False),
        _g("GET", "", _GM),
        _g("GET", "/{rest+}", _GM, spec=False),
        Route("GET", _A + "/user", None, "", untrusted_output=False, description="the agent's own account", spec=False),
        Route("GET", _A + "/version", None, "", untrusted_output=False),
        Route("GET", _A + "/metadata", None, "", untrusted_output=False),
        Route("GET", _A + "/projects", None, "", description="the projects the agent can see; results are untrusted"),
        Route("GET", _A + "/groups", None, "", description="the groups the agent can see; results are untrusted"),
        _p("POST", "/repository/blobs/batch", _RD, effect="read"),
        _p("POST", "/ci/lint", _RD, effect="read", description="validates CI configuration"),
        # Creating projects and groups.
        Route(
            "POST",
            _A + "/projects",
            _GPC,
            "group:{group}",
            body=_new_project,
            description="in the group namespace_id names; without one, in the agent's own namespace (no check)",
        ),
        Route("POST", _A + "/groups", _GA, "group:{group}", body=_new_group, description="a subgroup checks group.admin on its parent"),
        # The project itself.
        _p("PUT", "", _ADM),
        _p("DELETE", "", _DEL),
        _p("POST", "/fork", _RD, _fork, description="into the agent's own namespace; a fork into a group is refused"),
        _p("POST", "/star", _RD),
        _p("POST", "/unstar", _RD),
        _p("POST", "/housekeeping", _ADM),
        _p("POST", "/repository_size", _ADM),
        _p("POST", "/export", _ADM, sends_out=True, description="may upload the export to a URL in the body"),
        _p("POST", "/export_relations", _ADM),
        _p("GET", "/export/{rest+}", _ADM, spec=False),
        _p("GET", "/export_relations/{rest+}", _ADM, spec=False),
        # Issues.
        _p("POST", "/issues", _IC, sends_out=True),
        _p("PUT", "/issues/{issue_iid}", _IE),
        _p("PUT", "/issues/{issue_iid}/reorder", _IE),
        _p("POST", "/issues/{issue_iid}/links", _IE, _issue_link),
        _p("DELETE", "/issues/{issue_iid}/links/{issue_link_id}", _IE),
    ]
    for sub in ("add_spent_time", "reset_spent_time", "reset_time_estimate", "time_estimate", "metric_images", "metric_images/authorize"):
        out.append(_p("POST", f"/issues/{{issue_iid}}/{sub}", _IE))
    out += [
        _p("PUT", "/issues/{issue_iid}/metric_images/{metric_image_id}", _IE),
        _p("DELETE", "/issues/{issue_iid}/metric_images/{metric_image_id}", _IE),
    ]
    # Merge requests.
    m = "/merge_requests/{merge_request_iid}"
    out += [
        _p("POST", "/merge_requests", _MC, _mr_create, sends_out=True),
        _p("PUT", m, _MC, description="editing a merge request checks what creating one does (Developer)"),
        _p("PUT", m + "/merge", _MERGE, _merge, description="the target branch is not in the request: the project's protected branches make it unknown"),
        _p("POST", m + "/cancel_merge_when_pipeline_succeeds", _MERGE),
        _p("POST", m + "/approve", _MA),
        _p("POST", m + "/unapprove", _MA),
        _p("POST", m + "/approvals", _ADM),
        _p("PUT", m + "/reset_approvals", _ADM),
        _p("POST", m + "/pipelines", _RUN),
        _p("POST", m + "/context_commits", _MC),
        _p("DELETE", m + "/context_commits", _MC),
        _p("POST", m + "/draft_notes", _RD),
        _p("PUT", m + "/draft_notes/{draft_note_id}", _RD),
        _p("DELETE", m + "/draft_notes/{draft_note_id}", _RD),
        _p("PUT", m + "/draft_notes/{draft_note_id}/publish", _RD, sends_out=True),
        _p("POST", m + "/draft_notes/bulk_publish", _RD, sends_out=True),
    ]
    for sub in ("add_spent_time", "reset_spent_time", "reset_time_estimate", "time_estimate"):
        out.append(_p("POST", f"{m}/{sub}", _MC))
    # Comments, threads, emoji and subscriptions on issues, merge requests and snippets.
    for kind, iid, edit in (("issues", "{issue_iid}", _IE), ("merge_requests", "{merge_request_iid}", _MC), ("snippets", "{snippet_id}", _ADM)):
        base = f"/{kind}/{iid}"
        out += [
            _p("POST", base + "/award_emoji", _RD),
            _p("DELETE", base + "/award_emoji/{award_id}", _RD),
            _p("POST", base + "/notes/{note_id}/award_emoji", _RD),
            _p("DELETE", base + "/notes/{note_id}/award_emoji/{award_id}", _RD),
            _p("POST", base + "/notes", _RD, sends_out=True, spec=False),
            _p("PUT", base + "/notes/{note_id}", edit, sends_out=True, spec=False),
            _p("DELETE", base + "/notes/{note_id}", _ADM, spec=False),
            _p("POST", base + "/discussions", _RD, sends_out=True, spec=False),
            _p("POST", base + "/discussions/{discussion_id}/notes", _RD, sends_out=True, spec=False),
            _p("PUT", base + "/discussions/{discussion_id}/notes/{note_id}", edit, sends_out=True, spec=False),
            _p("DELETE", base + "/discussions/{discussion_id}/notes/{note_id}", _ADM, spec=False),
        ]
        if kind != "snippets":
            out += [
                _p("PUT", base + "/discussions/{discussion_id}", edit, spec=False, description="resolve or unresolve a thread"),
                _p("POST", base + "/subscribe", _RD, spec=False),
                _p("POST", base + "/unsubscribe", _RD, spec=False),
                _p("POST", base + "/todo", _RD, spec=False),
            ]
    # The repository: every write that lands on a branch names it.
    out += [
        _p("POST", "/repository/branches", _PUSH, _body_branch("branch"), resource=_PROJECT_AT),
        _p("DELETE", "/repository/branches/{branch}", _PUSH, _path_branch, resource=_PROJECT_AT),
        _p("PUT", "/repository/branches/{branch}/protect", _BP),
        _p("PUT", "/repository/branches/{branch}/unprotect", _BP),
        _p("DELETE", "/repository/merged_branches", _PUSH, description="protected branches are kept by GitLab; the project's rules make it unknown"),
        _p("POST", "/repository/commits", _PUSH, _body_branch("branch"), resource=_PROJECT_AT, sends_out=True),
        _p("POST", "/repository/commits/{sha}/cherry_pick", _PUSH, _body_branch("branch"), resource=_PROJECT_AT),
        _p("POST", "/repository/commits/{sha}/revert", _PUSH, _body_branch("branch"), resource=_PROJECT_AT),
        _p("POST", "/repository/commits/{sha}/comments", _RD, sends_out=True),
        _p("POST", "/repository/files/{file_path}", _PUSH, _body_branch("branch"), resource=_PROJECT_AT, sends_out=True),
        _p("PUT", "/repository/files/{file_path}", _PUSH, _body_branch("branch"), resource=_PROJECT_AT, sends_out=True),
        _p("DELETE", "/repository/files/{file_path}", _PUSH, _body_branch("branch"), resource=_PROJECT_AT),
        _p("PUT", "/repository/submodules/{submodule}", _PUSH, _body_branch("branch"), resource=_PROJECT_AT),
        _p("POST", "/repository/changelog", _PUSH, _body_branch("branch", required=False), resource=_PROJECT_AT),
    ]
    out += _subtree(_p, "/protected_branches", _BP, _RD)
    out += _subtree(_p, "/protected_tags", _BP, _RD)
    # Members.
    out += [
        _p("POST", "/members", _MEM, _new_member, description="not the Owner role, not a custom role"),
        _p("GET", "/invitations", _MEM),
        _p("GET", "/invitations/{rest+}", _MEM, spec=False),
        _p("POST", "/invitations", _MEM, _new_member, sends_out=True, description="emails the invitee"),
        _p("PUT", "/invitations/{email}", _MEM, _new_member),
        _p("DELETE", "/invitations/{email}", _MEM),
        _p("GET", "/access_requests", _MEM),
        _p("PUT", "/access_requests/{user_id}/approve", _MEM, _new_member),
        _p("DELETE", "/access_requests/{user_id}", _MEM),
        _p("DELETE", "/share/{group_id}", _MEM),
    ]
    # CI/CD.
    out += _subtree(_p, "/variables", _VAR)
    out += _subtree(_p, "/runners", _RUNNER)
    out += [
        _p("POST", "/pipeline", _RUN, description="pipeline.run does not evaluate protected refs"),
        _p("POST", "/pipelines/{pipeline_id}/cancel", _RUN),
        _p("POST", "/pipelines/{pipeline_id}/retry", _RUN),
        _p("PUT", "/pipelines/{pipeline_id}/metadata", _RUN),
        _p("POST", "/jobs/{job_id}/play", _RUN),
        _p("POST", "/jobs/{job_id}/retry", _RUN),
        _p("POST", "/jobs/{job_id}/cancel", _RUN),
        _p("POST", "/jobs/{job_id}/artifacts/keep", _RUN),
        _p("POST", "/jobs/{job_id}/erase", _ADM),
        _p("DELETE", "/jobs/{job_id}/artifacts", _ADM),
        _p("DELETE", "/artifacts", _ADM),
        _p("POST", "/statuses/{sha}", _RUN),
        _p("POST", "/pipeline_schedules", _RUN),
        _p("PUT", "/pipeline_schedules/{pipeline_schedule_id}", _RUN),
        _p("DELETE", "/pipeline_schedules/{pipeline_schedule_id}", _RUN),
        _p("POST", "/pipeline_schedules/{pipeline_schedule_id}/play", _RUN),
        _p("POST", "/pipeline_schedules/{pipeline_schedule_id}/take_ownership", _ADM),
        _p("POST", "/pipeline_schedules/{pipeline_schedule_id}/variables", _VAR),
        _p("PUT", "/pipeline_schedules/{pipeline_schedule_id}/variables/{key}", _VAR),
        _p("DELETE", "/pipeline_schedules/{pipeline_schedule_id}/variables/{key}", _VAR),
        # A schedule's own page lists its variables with their values: read as the variables are.
        _p("GET", "/pipeline_schedules/{pipeline_schedule_id}", _VAR),
    ]
    # Settings, credentials and integrations: Maintainer, reads included where they hold secrets.
    for sub in ("/hooks", "/integrations", "/services", "/remote_mirrors"):
        out += _subtree(_p, sub, _ADM, sends_out=True)
    for sub in ("/triggers", "/deploy_keys", "/deploy_tokens", "/access_tokens", "/secure_files", "/terraform/state", "/cluster_agents", "/clusters"):
        out += _subtree(_p, sub, _ADM)
    for sub in (
        "/badges",
        "/environments",
        "/feature_flags",
        "/feature_flags_user_lists",
        "/freeze_periods",
        "/error_tracking",
        "/pages",
        "/registry/protection",
        "/packages/protection",
        "/terraform/state_protection_rules",
        "/job_token_scope",
        "/resource_groups",
        "/debian_distributions",
        "/wikis",
        "/snippets",
        "/alert_management_alerts",
    ):
        out += _subtree(_p, sub, _ADM, _RD)
    out += [
        _p("POST", "/catalog/publish", _ADM),
        _p("POST", "/deployments", _ADM),
        _p("PUT", "/deployments/{deployment_id}", _ADM),
        _p("DELETE", "/deployments/{deployment_id}", _ADM),
        _p("POST", "/access_tokens/self/rotate", None, _refuse(_SELF_ROTATE)),
        _p("DELETE", "/registry/repositories/{repository_id}", _ADM),
        _p("DELETE", "/registry/repositories/{repository_id}/tags", _ADM),
        _p("DELETE", "/registry/repositories/{repository_id}/tags/{tag_name}", _ADM),
        _p("DELETE", "/packages/{package_id}", _ADM),
        _p("DELETE", "/packages/{package_id}/package_files/{package_file_id}", _ADM),
        _p("POST", "/uploads", _RD, description="an attachment for a comment or description"),
        _p("POST", "/uploads/authorize", _RD),
        _p("DELETE", "/uploads/{upload_id}", _ADM),
        _p("DELETE", "/uploads/{secret}/{filename}", _ADM),
        # Releases that do not create a tag.
        _p("POST", "/releases", _PUSH, _release),
        _p("PUT", "/releases/{tag_name}", _PUSH),
        _p("DELETE", "/releases/{tag_name}", _ADM),
        _p("POST", "/releases/{tag_name}/assets/links", _PUSH),
        _p("PUT", "/releases/{tag_name}/assets/links/{link_id}", _PUSH),
        _p("DELETE", "/releases/{tag_name}/assets/links/{link_id}", _PUSH),
        _p("POST", "/releases/{tag_name}/evidence", _PUSH),
    ]
    # Groups.
    out += [
        _g("PUT", "", _GA),
        _g("DELETE", "", _GA),
        _g("POST", "/archive", _GA),
        _g("POST", "/unarchive", _GA),
        _g("POST", "/restore", _GA),
        _g("POST", "/ldap_sync", _GA),
        _g("POST", "/export", _GA),
        _g("POST", "/export_relations", _GA),
        _g("GET", "/export/{rest+}", _GA, spec=False),
        _g("GET", "/export_relations/{rest+}", _GA, spec=False),
        _g("POST", "/members", _GA),
        _g("POST", "/members/approve_all", _GA),
        _g("PUT", "/members/{member_id}/approve", _GA),
        _g("PUT", "/members/{user_id}", _GA),
        _g("DELETE", "/members/{user_id}", _GA),
        _g("POST", "/members/{user_id}/override", _GA),
        _g("DELETE", "/members/{user_id}/override", _GA),
        _g("PUT", "/members/{user_id}/state", _GA),
        _g("DELETE", "/billable_members/{user_id}", _GA),
        _g("GET", "/invitations", _GA),
        _g("GET", "/invitations/{rest+}", _GA, spec=False),
        _g("POST", "/invitations", _GA, sends_out=True),
        _g("PUT", "/invitations/{email}", _GA),
        _g("DELETE", "/invitations/{email}", _GA),
        _g("GET", "/access_requests", _GA),
        _g("PUT", "/access_requests/{user_id}/approve", _GA),
        _g("DELETE", "/access_requests/{user_id}", _GA),
        _g("DELETE", "/share/{group_id}", _GA),
        _g("POST", "/epics/{epic_iid}/award_emoji", _GM),
        _g("DELETE", "/epics/{epic_iid}/award_emoji/{award_id}", _GM),
        _g("POST", "/epics/{epic_iid}/notes/{note_id}/award_emoji", _GM),
        _g("DELETE", "/epics/{epic_iid}/notes/{note_id}/award_emoji/{award_id}", _GM),
        _g("POST", "/uploads", _GM),
        _g("POST", "/uploads/authorize", _GM),
        _g("DELETE", "/uploads/{upload_id}", _GA),
        _g("DELETE", "/uploads/{secret}/{filename}", _GA),
        _g("DELETE", "/dependency_proxy/cache", _GA),
        _g("POST", "/runners/reset_registration_token", _GA),
        _g("POST", "/access_tokens/self/rotate", None, _refuse(_SELF_ROTATE)),
    ]
    for sub in ("/hooks", "/integrations", "/services"):
        out += _subtree(_g, sub, _GA, sends_out=True)
    for sub in ("/variables", "/deploy_tokens", "/access_tokens", "/runners", "/clusters", "/ssh_certificates", "/audit_events"):
        out += _subtree(_g, sub, _GA)
    for sub in ("/badges", "/wikis", "/-/debian_distributions"):
        out += _subtree(_g, sub, _GA, _GM)
    return out


def _locate(settings: Any) -> list[tuple[str, str]]:
    """The whole instance at its url (default https://gitlab.com): the API
    under /api/v4, and the rest of the host, so a write outside the API is
    refused rather than let through."""
    url = (settings.get("url") if hasattr(settings, "get") else "") or "https://gitlab.com"
    ep = endpoint_of_url(url)
    return [ep] if ep is not None else []


_OWNER = "needs the Owner role (or an administrator); the integration's only Owner action is project.delete"
_SECOND = "reaches a second project or group named in the request; toolpass checks one target per request"
_TAGS = "tags may be protected, and the integration evaluates protected branches only"
_PACKAGES = "package registry protocols, used by package managers with their own tokens"
_ADMIN = "an instance administrator's job, done outside agents"

ROUTES = RouteTable(
    "gitlab",
    _routes(),
    claims=(_A + "/projects", _A + "/groups"),
    unrouted={
        ("POST", _A + "/projects/user/{user_id}"): _ADMIN,
        ("POST", _A + "/projects/import"): "imports a project from an uploaded export: not checkable",
        ("POST", _A + "/projects/import/authorize"): "imports a project from an uploaded export: not checkable",
        ("POST", _A + "/projects/import-relation"): "imports into a project from an uploaded export: not checkable",
        ("POST", _A + "/projects/import-relation/authorize"): "imports into a project from an uploaded export: not checkable",
        ("POST", _A + "/projects/remote-import"): "imports a project from a remote export: not checkable",
        ("POST", _A + "/projects/remote-import-s3"): "imports a project from a remote export: not checkable",
        ("POST", _A + "/groups/import"): "imports a group from an uploaded export: not checkable",
        ("POST", _A + "/groups/import/authorize"): "imports a group from an uploaded export: not checkable",
        ("DELETE", _P + "/issues/{issue_iid}"): _OWNER,
        ("POST", _P + "/issues/{issue_iid}/move"): "moves the issue into another project named in the body; " + _SECOND,
        ("POST", _P + "/issues/{issue_iid}/clone"): "copies the issue into another project named in the body; " + _SECOND,
        ("DELETE", _P + "/merge_requests/{merge_request_iid}"): _OWNER,
        ("PUT", _P + "/merge_requests/{merge_request_iid}/rebase"): "pushes to the merge request's source branch, which the request does not name",
        ("DELETE", _P + "/pipelines/{pipeline_id}"): _OWNER,
        ("POST", _P + "/archive"): _OWNER,
        ("POST", _P + "/unarchive"): _OWNER,
        ("POST", _P + "/restore"): _OWNER,
        ("PUT", _P + "/transfer"): "moves the project into another namespace; " + _SECOND,
        ("DELETE", _P + "/fork"): _OWNER,
        ("POST", _P + "/fork/{forked_from_id}"): _ADMIN,
        ("POST", _P + "/share"): "shares the project with a group named in the body; " + _SECOND,
        ("POST", _P + "/import_project_members/{project_id}"): "copies the members of another project; " + _SECOND,
        ("POST", _P + "/import/git"): "imports a repository from elsewhere: not checkable",
        ("PUT", _P + "/members/{user_id}"): "the member may be an Owner, whom only an Owner may change; member.manage is Maintainer",
        ("DELETE", _P + "/members/{user_id}"): "the member may be an Owner, whom only an Owner may remove; member.manage is Maintainer",
        ("POST", _P + "/access_requests"): "requests access for the agent's own account",
        ("PUT", _P + "/custom_attributes/{key}"): _ADMIN,
        ("DELETE", _P + "/custom_attributes/{key}"): _ADMIN,
        ("POST", _P + "/repository/tags"): _TAGS,
        ("DELETE", _P + "/repository/tags/{tag_name}"): _TAGS,
        ("POST", _P + "/deployments/{deployment_id}/approval"): "deployment approval rules name approvers the integration does not evaluate",
        ("POST", _P + "/create_ci_config"): "not documented; not routed",
        ("POST", _P + "/(ref/{ref}/)trigger/pipeline"): "authenticated by a pipeline trigger token, not a user",
        ("*", _P + "/packages/**"): _PACKAGES,
        ("POST", _G + "/projects/{project_id}"): "moves a project into the group; " + _SECOND,
        ("POST", _G + "/transfer"): "moves the group under another group; " + _SECOND,
        ("POST", _G + "/transfer_to_organization"): "moves the group into an organization; " + _SECOND,
        ("POST", _G + "/share"): "shares the group with a group named in the body; " + _SECOND,
        ("DELETE", _G + "/shared_projects/{project_id}"): "changes a project's access as well as the group's; " + _SECOND,
        ("POST", _G + "/access_requests"): "requests access for the agent's own account",
        ("PUT", _G + "/custom_attributes/{key}"): _ADMIN,
        ("DELETE", _G + "/custom_attributes/{key}"): _ADMIN,
        ("POST", _G + "/placeholder_reassignments"): "reassigns imported contributions: an import's job",
        ("POST", _G + "/placeholder_reassignments/authorize"): "reassigns imported contributions: an import's job",
        ("*", _G + "/-/packages/**"): _PACKAGES,
    },
    locate=_locate,
)

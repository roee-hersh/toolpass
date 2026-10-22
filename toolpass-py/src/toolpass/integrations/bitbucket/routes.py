"""Bitbucket requests, mapped to the bitbucket integration's actions, for
both editions.

Cloud (``api.bitbucket.org/2.0``): the integration checks repositories and
projects of the connection's workspace (``repo:<slug>``, ``project:<key>``,
``workspace``), so a request must name that workspace in its path; one that
names another workspace is refused. Data Center (``<url>/rest/api/1.0`` or
``latest``): ``repo:<PROJECT>/<slug>`` and ``project:<KEY>``.

The integration checks branch restrictions only when the resource names the
branch (``repo:<slug>@<branch>``), and grants repo.push or pr.merge on write
access alone otherwise. So every request that lands on a branch must name
it: creating or deleting a branch, committing files. Merging a pull request
does not name its destination branch, so it is refused rather than checked
without the branch's merge restrictions.

Bodies are read as JSON objects (a name given twice is refused), and file
commits as URL-encoded or multipart forms with exactly one ``branch``
field. Forks, which land in a project named in the body, and Connect app
properties are not routed.
"""

from __future__ import annotations

import dataclasses
import email.parser
import email.policy
import json
import re
import urllib.parse
from collections.abc import Mapping
from typing import Any

from toolpass.core.routes import Extractor, Observed, Request, Route, RouteTable, endpoint_of_url
from toolpass.integrations.bitbucket.actions import valid_branch, valid_slug

__all__ = ["ROUTES", "BitbucketRoutes"]

DEFAULT_CLOUD_URL = "https://api.bitbucket.org"

_R = "/repositories/{workspace}/{repo_slug}"
_W = "/workspaces/{workspace}"
_REPO = "repo:{repo_slug}"
_REPO_AT = "repo:{repo_slug}{at}"
_CPROJECT = "project:{project_key}"
_WORKSPACE = "workspace"
_DREPO = "repo:{projectKey}/{repositorySlug}"
_DREPO_AT = "repo:{projectKey}/{repositorySlug}{at}"
_DPROJECT = "project:{projectKey}"

_RR, _RP, _MERGE, _RA = "repo.read", "repo.push", "pr.merge", "repo.admin"
_PR, _PA, _RC = "project.read", "project.admin", "repo.create"
_WM, _WA = "workspace.member", "workspace.admin"

_WRITES = ("POST", "PUT", "PATCH", "DELETE")


# -- bodies ------------------------------------------------------------------------


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in pairs:
        if k in out:
            raise ValueError(f"{k} is given more than once")
        out[k] = v
    return out


def _json(r: Request) -> tuple[dict[str, Any], str]:
    """The body as a JSON object ({} when there is none), or a reason it cannot be read exactly."""
    if not r.body:
        return {}, ""
    if r.content_type != "application/json":
        return {}, f"a body of type {r.content_type or 'unknown'!r} is not read here; send JSON"
    try:
        doc = json.loads(r.body.decode("utf-8"), object_pairs_hook=_unique_pairs)
    except (ValueError, UnicodeDecodeError):
        return {}, "the JSON body cannot be read, or gives a name more than once"
    if not isinstance(doc, dict):
        return {}, "the JSON body is not an object"
    return doc, ""


def _form_field(r: Request, name: str) -> tuple[str | None, str]:
    """One field of a URL-encoded or multipart form: (value or None, reason it cannot be used)."""
    if not r.body:
        return None, ""
    ct = r.content_type
    if ct == "application/x-www-form-urlencoded":
        try:
            text = r.body.decode("utf-8")
            if ";" in text:
                return None, "the form contains a raw ';'"
            pairs = urllib.parse.parse_qsl(text, keep_blank_values=True, strict_parsing=True, errors="strict")
        except (ValueError, UnicodeDecodeError):
            return None, "the form cannot be read exactly"
        values = [v for k, v in pairs if k == name]
        if any((k.lower() == name and k != name) or k.startswith(name + "[") for k, _ in pairs):
            return None, f"the form gives {name} in another spelling"
    elif ct == "multipart/form-data":
        values, why = _multipart_values(r, name)
        if why:
            return None, why
    else:
        return None, f"a body of type {ct or 'unknown'!r} is not read here; send a URL-encoded or multipart form"
    if len(values) > 1:
        return None, f"the form gives {name} more than once"
    return (values[0] if values else None), ""


def _multipart_values(r: Request, name: str) -> tuple[list[str], str]:
    """The values of a non-file field of a multipart body, read strictly: a
    part that names the field with a file name, an encoded name or a
    transfer encoding, or a body the parser finds defects in, is refused."""
    head = b"Content-Type: " + r.headers.get("content-type", "").encode("latin-1", "replace") + b"\r\n\r\n"
    msg = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(head + (r.body or b""))
    if not msg.is_multipart() or msg.defects:
        return [], "the multipart body cannot be read exactly"
    out: list[str] = []
    for part in msg.iter_parts():
        if part.defects:
            return [], "the multipart body cannot be read exactly"
        if len(part.get_all("content-disposition", [])) != 1:
            return [], "a multipart part has no or several Content-Disposition headers"
        cd = part.get("content-disposition", "")
        if "name*" in cd.lower():
            return [], "a multipart field name is encoded; toolpass reads plain names only"
        field_name = part.get_param("name", header="content-disposition")
        if not isinstance(field_name, str):
            continue
        if field_name.lower() != name.lower():
            continue
        if field_name != name or part.get_filename() is not None or part.get("content-transfer-encoding"):
            return [], f"the multipart field {name} is not a plain text field"
        payload = part.get_payload(decode=True)
        try:
            out.append(payload.decode("utf-8") if isinstance(payload, bytes) else "")
        except UnicodeDecodeError:
            return [], f"the multipart field {name} is not UTF-8"
    return out, ""


# -- extractors ----------------------------------------------------------------------


def _chain(*extractors: Extractor) -> Extractor:
    """Run extractors in order; the first refusal wins."""

    def extract(r: Request, vars_: Mapping[str, str]) -> dict[str, str]:
        out: dict[str, str] = {}
        merged = dict(vars_)
        for e in extractors:
            res = e(r, merged)
            got = dict(res) if res is not None else {"_unresolved": "the request does not name a resource toolpass can check"}
            if "_unresolved" in got:
                return {"_unresolved": got["_unresolved"]}
            out.update(got)
            merged.update(got)
        return out

    return extract


def _slugs(*names: str) -> Extractor:
    """The path's slugs and keys must be ones the integration accepts."""

    def extract(r: Request, vars_: Mapping[str, str]) -> dict[str, str]:
        for n in names:
            if not valid_slug(vars_.get(n, "")):
                return {"_unresolved": f"{n} {vars_.get(n, '')!r} is not a slug or key toolpass can check"}
        return {}

    return extract


def _at(branch: str, *, full_refs: bool = False) -> dict[str, str]:
    """``@branch`` for the resource. Data Center takes a full ref (``refs/heads/x``) where it takes a branch; Cloud does not."""
    if full_refs and branch.startswith("refs/heads/"):
        branch = branch[len("refs/heads/") :]
    if branch.startswith("refs/"):
        return {"_unresolved": f"{branch!r} is a ref, not a branch name"}
    if "@" in branch or not valid_branch(branch):
        return {"_unresolved": f"the branch {branch!r} is not a branch name toolpass can check"}
    return {"at": "@" + branch}


def _refuse(reason: str) -> Extractor:
    def extract(r: Request, vars_: Mapping[str, str]) -> dict[str, str]:
        return {"_unresolved": reason}

    return extract


def _json_branch(*path: str, full_refs: bool = False) -> Extractor:
    """The branch a write lands on, from a member of the JSON body."""

    def extract(r: Request, vars_: Mapping[str, str]) -> dict[str, str]:
        doc, why = _json(r)
        if why:
            return {"_unresolved": why}
        v: Any = doc
        for p in path:
            v = v.get(p) if isinstance(v, dict) else None
        if not isinstance(v, str) or not v:
            return {"_unresolved": f"the request does not name the branch ({'.'.join(path)})"}
        return _at(v, full_refs=full_refs)

    return extract


def _path_branch(r: Request, vars_: Mapping[str, str]) -> dict[str, str]:
    return _at(vars_.get("name", ""))


def _form_branch(r: Request, vars_: Mapping[str, str]) -> dict[str, str]:
    """A file commit lands on the form's branch; without one, on the main branch, which the request does not name."""
    if any(k == "branch" or k.lower().startswith("branch") for k in r.params()):
        return {"_unresolved": "the branch is given in the query; give it once, in the form"}
    branch, why = _form_field(r, "branch")
    if why:
        return {"_unresolved": why}
    if not branch:
        return {"_unresolved": "the commit does not name its branch (branch); name it so its restrictions can be checked"}
    return _at(branch)


def _without(*names: str) -> Extractor:
    """JSON members that would reach another project or repository: refused."""

    def extract(r: Request, vars_: Mapping[str, str]) -> dict[str, str]:
        doc, why = _json(r)
        if why:
            return {"_unresolved": why}
        for n in names:
            if n in doc:
                return {"_unresolved": f"{n} in the body reaches another project or repository; toolpass refuses it"}
        return {}

    return extract


def _project_key(doc: Mapping[str, Any]) -> str | None:
    """``project: {"key": K}`` and nothing beside the key that could name another project."""
    project = doc.get("project")
    if isinstance(project, dict) and set(project) - {"type"} == {"key"} and isinstance(project.get("key"), str):
        key = project["key"]
        return key if valid_slug(key) else None
    return None


def _cloud_new_repo(r: Request, vars_: Mapping[str, str]) -> dict[str, str]:
    """Creating a repository needs create-repo on the project the body names by key."""
    doc, why = _json(r)
    if why:
        return {"_unresolved": why}
    key = _project_key(doc)
    if key is None:
        return {"_unresolved": "the request does not name the project by its key (project.key); toolpass checks repo.create on it"}
    return {"project_key": key}


def _cloud_new_pr(r: Request, vars_: Mapping[str, str]) -> dict[str, str]:
    """A pull request into this repository, from a branch of this repository."""
    doc, why = _json(r)
    if why:
        return {"_unresolved": why}
    this = f"{vars_.get('workspace')}/{vars_.get('repo_slug')}"
    for side in ("source", "destination"):
        part = doc.get(side)
        repo = part.get("repository") if isinstance(part, dict) else None
        if repo is None:
            continue
        if not isinstance(repo, dict) or set(repo) - {"type"} != {"full_name"} or repo.get("full_name") != this:
            return {"_unresolved": f"{side}.repository names another repository than {this}; toolpass checks one repository per request"}
    return {}


def _dc_new_pr(r: Request, vars_: Mapping[str, str]) -> dict[str, str]:
    """A pull request between branches of this repository."""
    doc, why = _json(r)
    if why:
        return {"_unresolved": why}
    key, slug = vars_.get("projectKey"), vars_.get("repositorySlug")
    for side in ("fromRef", "toRef"):
        ref = doc.get(side)
        repo = ref.get("repository") if isinstance(ref, dict) else None
        if repo is None:
            continue
        project = repo.get("project") if isinstance(repo, dict) else None
        if (
            not isinstance(repo, dict)
            or not isinstance(project, dict)
            or set(repo) != {"slug", "project"}
            or set(project) != {"key"}
            or repo.get("slug") != slug
            or project.get("key") != key
        ):
            return {"_unresolved": f"{side}.repository names another repository than {key}/{slug}; toolpass checks one repository per request"}
    return {}


def _dc_new_repo(r: Request, vars_: Mapping[str, str]) -> dict[str, str]:
    doc, why = _json(r)
    if why:
        return {"_unresolved": why}
    if "project" in doc:
        return {"_unresolved": "the body names a project; the repository is created in the one in the path"}
    return {}


_MERGE_REFUSED = (
    "the merge does not name the destination branch, so its merge restrictions cannot be checked; toolpass refuses it rather than check write access alone"
)


# -- routes ----------------------------------------------------------------------------


def _c(method: str, sub: str, action: str | None, *more: Extractor, resource: str = _REPO, **kw: Any) -> Route:
    """A Cloud repository route."""
    return Route(method, _R + sub, action, resource, body=_chain(_slugs("repo_slug"), *more), **kw)


def _cw(method: str, sub: str, action: str | None, *more: Extractor, resource: str = _WORKSPACE, **kw: Any) -> Route:
    """A Cloud workspace route."""
    return Route(method, _W + sub, action, resource, body=_chain(*more) if more else None, **kw)


def _cp(method: str, sub: str, action: str | None, *more: Extractor, **kw: Any) -> Route:
    """A Cloud project route."""
    return Route(method, _W + "/projects/{project_key}" + sub, action, _CPROJECT, body=_chain(_slugs("project_key"), *more), **kw)


def _subtree(make: Any, sub: str, write: str, read: str | None = None, *, sends_out: bool = False) -> list[Route]:
    """Every request under ``sub``: writes check ``write``, reads ``read`` (``write`` when None)."""
    out = [make("GET", sub, read or write, spec=False), make("GET", sub + "/{rest+}", read or write, spec=False)]
    for m in _WRITES:
        out.append(make(m, sub, write, sends_out=sends_out, spec=False))
        out.append(make(m, sub + "/{rest+}", write, sends_out=sends_out, spec=False))
    return out


def _cloud() -> list[Route]:
    out: list[Route] = [
        # Reads.
        Route("GET", "/repositories/{workspace}", _WM, _WORKSPACE, description="the workspace's repositories, as the agent sees them"),
        _c("GET", "", _RR),
        _c("GET", "/{rest+}", _RR, spec=False),
        _cw("GET", "", _WM),
        _cw("GET", "/{rest+}", _WM, spec=False),
        _cw("GET", "/permissions", _WA),
        _cw("GET", "/permissions/{rest+}", _WA, spec=False),
        _cp("GET", "", _PR),
        _cp("GET", "/{rest+}", _PR, spec=False),
        Route("GET", "/user", None, "", untrusted_output=False, description="the agent's own account"),
        Route("GET", "/user/{rest+}", None, "", spec=False, description="the agent's own account"),
        _c("POST", "/commits", _RR, effect="read", description="lists commits; the filters are in the body"),
        _c("POST", "/commits/{revision}", _RR, effect="read", description="lists commits; the filters are in the body"),
        # The repository.
        _c("POST", "", _RC, _cloud_new_repo, resource=_CPROJECT),
        _c("PUT", "", _RA, _without("project"), description="updates (or creates) the repository; moving it to a project is refused"),
        _c("DELETE", "", _RA),
        _c("PUT", "/branching-model/settings", _RA),
        _c("PUT", "/override-settings", _RA),
        _c("PUT", "/default-reviewers/{target_username}", _RA),
        _c("DELETE", "/default-reviewers/{target_username}", _RA),
        # Branches, tags, commits. Tags are not branch-restricted on Cloud.
        _c("POST", "/refs/branches", _RP, _json_branch("name"), resource=_REPO_AT),
        _c("DELETE", "/refs/branches/{name+}", _RP, _path_branch, resource=_REPO_AT),
        _c("POST", "/refs/tags", _RP),
        _c("DELETE", "/refs/tags/{name+}", _RP),
        _c("POST", "/src", _RP, _form_branch, resource=_REPO_AT, sends_out=True),
        _c("POST", "/downloads", _RP, sends_out=True),
        _c("DELETE", "/downloads/{filename}", _RP),
        _c("POST", "/commit/{commit}/approve", _RR),
        _c("DELETE", "/commit/{commit}/approve", _RR),
        _c("POST", "/commit/{commit}/comments", _RR, sends_out=True),
        _c("PUT", "/commit/{commit}/comments/{comment_id}", _RA, sends_out=True),
        _c("DELETE", "/commit/{commit}/comments/{comment_id}", _RA),
        _c("POST", "/commit/{commit}/statuses/build", _RP),
        _c("PUT", "/commit/{commit}/statuses/build/{key}", _RP),
        _c("PUT", "/commit/{commit}/reports/{reportId}", _RP),
        _c("DELETE", "/commit/{commit}/reports/{reportId}", _RP),
        _c("POST", "/commit/{commit}/reports/{reportId}/annotations", _RP),
        _c("PUT", "/commit/{commit}/reports/{reportId}/annotations/{annotationId}", _RP),
        _c("DELETE", "/commit/{commit}/reports/{reportId}/annotations/{annotationId}", _RP),
        # Pull requests.
        _c("POST", "/pullrequests", _RR, _cloud_new_pr, sends_out=True, description="opening a pull request needs read access"),
        _c("PUT", "/pullrequests/{pull_request_id}", _RP, sends_out=True),
        _c("POST", "/pullrequests/{pull_request_id}/merge", _MERGE, _refuse(_MERGE_REFUSED), resource=_REPO_AT),
        _c("POST", "/pullrequests/{pull_request_id}/decline", _RP),
        _c("POST", "/pullrequests/{pull_request_id}/approve", _RR),
        _c("DELETE", "/pullrequests/{pull_request_id}/approve", _RR),
        _c("POST", "/pullrequests/{pull_request_id}/request-changes", _RR),
        _c("DELETE", "/pullrequests/{pull_request_id}/request-changes", _RR),
        _c("POST", "/pullrequests/{pull_request_id}/comments", _RR, sends_out=True),
        _c("PUT", "/pullrequests/{pull_request_id}/comments/{comment_id}", _RA, sends_out=True),
        _c("DELETE", "/pullrequests/{pull_request_id}/comments/{comment_id}", _RA),
        _c("POST", "/pullrequests/{pull_request_id}/comments/{comment_id}/resolve", _RR),
        _c("DELETE", "/pullrequests/{pull_request_id}/comments/{comment_id}/resolve", _RR),
        _c("POST", "/pullrequests/{pull_request_id}/tasks", _RR),
        _c("PUT", "/pullrequests/{pull_request_id}/tasks/{task_id}", _RR),
        _c("DELETE", "/pullrequests/{pull_request_id}/tasks/{task_id}", _RP),
        _c("DELETE", "/merge-queues/{merge_queue_uuid}/queue/{pullrequest_id}", _RP),
        # Pipelines: write access runs them; branch restrictions do not govern pipelines.
        _c("POST", "/pipelines", _RP, description="deployment permissions of environments are not evaluated"),
        _c("POST", "/pipelines/{pipeline_uuid}/stopPipeline", _RP),
        _c("POST", "/environments", _RA),
        _c("DELETE", "/environments/{environment_uuid}", _RA),
        _c("POST", "/environments/{environment_uuid}/changes", _RA),
    ]
    out += _subtree(_c, "/branch-restrictions", _RA, _RR)
    out += _subtree(_c, "/pipelines_config", _RA, _RR)
    out += _subtree(_c, "/pipelines-config", _RA, _RR)
    out += _subtree(_c, "/hooks", _RA, sends_out=True)
    for sub in ("/deploy-keys", "/permissions-config", "/deployments_config", "/pipelines_config/variables", "/pipelines_config/ssh"):
        out += _subtree(_c, sub, _RA)
    # The workspace and its projects.
    out += [
        _cw("POST", "/projects", _WA, description="creating a project checks workspace.admin, the stricter answer"),
        _cp("PUT", "", _PA),
        _cp("DELETE", "", _PA),
        _cp("PUT", "/branching-model/settings", _PA),
        _cp("PUT", "/default-reviewers/{selected_user}", _PA),
        _cp("DELETE", "/default-reviewers/{selected_user}", _PA),
    ]
    out += _subtree(_cw, "/hooks", _WA, sends_out=True)
    out += _subtree(_cw, "/pipelines-config", _WA)
    for sub in ("/deploy-keys", "/permissions-config"):
        out += _subtree(_cp, sub, _PA)
    return out


def _dc(base: str, resource: str, keys: Extractor) -> Any:
    """A maker of Data Center routes under ``base`` (there is no Data Center description to check them against)."""

    def make(method: str, sub: str, action: str | None, *more: Extractor, **kw: Any) -> Route:
        kw["spec"] = False
        return Route(method, base + sub, action, kw.pop("resource", resource), body=_chain(keys, *more), **kw)

    return make


def _data_center() -> list[Route]:
    out: list[Route] = []
    for v in ("1.0", "latest"):
        d = f"/rest/api/{v}"
        dp = _dc(d + "/projects/{projectKey}", _DPROJECT, _slugs("projectKey"))
        dr = _dc(d + "/projects/{projectKey}/repos/{repositorySlug}", _DREPO, _slugs("projectKey", "repositorySlug"))
        bu = _dc(f"/rest/branch-utils/{v}/projects/{{projectKey}}/repos/{{repositorySlug}}", _DREPO_AT, _slugs("projectKey", "repositorySlug"))
        out += [
            Route("POST", d + "/projects", _WA, _WORKSPACE, spec=False, description="creating a project checks workspace.admin, the stricter answer"),
            dp("GET", "", _PR),
            dp("GET", "/{rest+}", _PR),
            dp("PUT", "", _PA),
            dp("DELETE", "", _PA),
            dp("POST", "/repos", _RC, _dc_new_repo),
            dr("GET", "", _RR),
            dr("GET", "/{rest+}", _RR),
            dr("POST", "", None, _refuse("a fork lands in a project named in the body; toolpass checks one target per request")),
            dr("PUT", "", _RA, _without("project"), description="moving the repository to another project is refused"),
            dr("DELETE", "", _RA),
            dr("PUT", "/browse/{path+}", _RP, _form_branch, resource=_DREPO_AT, sends_out=True),
            dr("PUT", "/branches/default", _RA),
            dr("PUT", "/default-branch", _RA),
            dr("POST", "/commits/{commitId}/builds", _RP),
            dr("POST", "/pull-requests", _RR, _dc_new_pr, sends_out=True, description="opening a pull request needs read access"),
            dr("PUT", "/pull-requests/{pullRequestId}", _RP, sends_out=True),
            dr("DELETE", "/pull-requests/{pullRequestId}", _RA),
            dr("POST", "/pull-requests/{pullRequestId}/merge", _MERGE, _refuse(_MERGE_REFUSED), resource=_DREPO_AT),
            dr("POST", "/pull-requests/{pullRequestId}/decline", _RP),
            dr("POST", "/pull-requests/{pullRequestId}/reopen", _RP),
            dr("POST", "/pull-requests/{pullRequestId}/approve", _RR),
            dr("DELETE", "/pull-requests/{pullRequestId}/approve", _RR),
            dr("POST", "/pull-requests/{pullRequestId}/participants", _RP),
            dr("PUT", "/pull-requests/{pullRequestId}/participants/{userSlug}", _RR, description="the reviewer's own status"),
            dr("DELETE", "/pull-requests/{pullRequestId}/participants/{userSlug}", _RP),
            bu("POST", "/branches", _RP, _json_branch("name", full_refs=True)),
            bu("DELETE", "/branches", _RP, _json_branch("name", full_refs=True)),
        ]
        for base in ("/pull-requests/{pullRequestId}/comments", "/pull-requests/{pullRequestId}/blocker-comments", "/commits/{commitId}/comments"):
            out += [
                dr("POST", base, _RR, sends_out=True),
                dr("PUT", base + "/{commentId}", _RA, sends_out=True),
                dr("DELETE", base + "/{commentId}", _RA),
            ]
        out += _subtree(dp, "/permissions", _PA)
        out += _subtree(dp, "/settings", _PA)
        out += _subtree(dp, "/webhooks", _PA, sends_out=True)
        out += _subtree(dr, "/permissions", _RA)
        out += _subtree(dr, "/settings", _RA)
        out += _subtree(dr, "/webhooks", _RA, sends_out=True)
    return out


_WS_PREFIX = re.compile(r"(.*/2\.0)/(?:repositories|workspaces)/([^/]+)")


class BitbucketRoutes(RouteTable):
    """The table, with the Cloud workspace held to the connection's.

    A Cloud connection's endpoints include ``/2.0/repositories/<workspace>``
    and ``/2.0/workspaces/<workspace>``, so the prefix a request arrives on
    says which workspace the connection checks; a route whose path names a
    workspace is unresolved unless it is that one."""

    def match(self, request: Request, prefix: str = "") -> Observed | None:
        m = _WS_PREFIX.fullmatch(prefix)
        workspace = None
        if m is not None:
            prefix, workspace = m.group(1), m.group(2)
        found = super().match(request, prefix)
        if found is None or "workspace" not in found.fields or found.fields["workspace"] == workspace:
            return found
        named = found.fields["workspace"]
        why = f"the request names workspace {named!r}; this connection checks its own workspace only"
        return dataclasses.replace(found, resource=None, unresolved=why)


def _locate(settings: Any) -> list[tuple[str, str]]:
    """Data Center: the whole instance at its url. Cloud: api.bitbucket.org,
    with the connection's workspace under /2.0 as its own endpoints (see
    BitbucketRoutes)."""
    get = settings.get if hasattr(settings, "get") else (lambda _k: "")
    if (get("edition") or "cloud") == "datacenter":
        ep = endpoint_of_url(get("url") or "")
        return [ep] if ep is not None else []
    ep = endpoint_of_url(get("url") or DEFAULT_CLOUD_URL)
    if ep is None:
        return []
    host, path = ep
    out = [(host, path), (host, path + "/2.0")]
    workspace = get("workspace") or ""
    if valid_slug(workspace):
        out += [(host, f"{path}/2.0/repositories/{workspace}"), (host, f"{path}/2.0/workspaces/{workspace}")]
    return out


_APP = "a Connect app's own properties: app-level, no user action to check"

ROUTES = BitbucketRoutes(
    "bitbucket",
    _cloud() + _data_center(),
    claims=("/repositories/{workspace}", "/workspaces/{workspace}"),
    unrouted={
        ("POST", _W + "/codesearch"): "searches code across every repository of the workspace; toolpass checks one repository per request",
        ("POST", _R + "/forks"): "a fork lands in a workspace and project named in the body; toolpass checks one target per request",
        ("*", _R + "/properties/{app_key}/{property_name}"): _APP,
        ("*", _R + "/commit/{commit}/properties/{app_key}/{property_name}"): _APP,
        ("*", _R + "/pullrequests/{pullrequest_id}/properties/{app_key}/{property_name}"): _APP,
        ("POST", "/rest/git/{version}/projects/{projectKey}/repos/{repositorySlug}/tags"): "Data Center ref restrictions can cover tags; not evaluated",
    },
    prefixes=("/2.0",),
    locate=_locate,
)

"""GitHub REST requests, mapped to the github integration's actions.

The permission ladder decides the action: a request that needs pull is
``repo.read``, triage ``repo.triage``, push ``repo.push``, admin
``repo.admin``; ``issue.create``, ``pr.create`` and ``pr.merge`` keep their
own names because the engine answers them with extra conditions (issues
enabled, forking allowed, branch rules). A request that carries a branch
(a push to ``refs/heads/x``, a protection rule) checks ``repo:o/r@x`` so
branch rules and protection apply.

Requests about the agent's own identity, search and rate limits need no
user permission (action None) and are allowed, audited. Mutations over
GraphQL are not routed: the operation is in the body and is refused until a
route reads it.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from toolpass.core.routes import Request, Route, RouteTable

__all__ = ["ROUTES"]

_REPO = "repo:{owner}/{repo}"
_REPO_AT = "repo:{owner}/{repo}{at}"
_ORG = "org:{org}"
_TEAM = "team:{org}/{team_slug}"


def _at(branch: str | None) -> dict[str, str]:
    return {"at": f"@{branch}" if branch else ""}


def _branch_of_ref(ref: str) -> str | None:
    for prefix in ("refs/heads/", "heads/"):
        if ref.startswith(prefix):
            return ref[len(prefix) :] or None
    return None


def _ref_body(r: Request, vars_: Mapping[str, str]) -> dict[str, str]:
    """POST /git/refs: the branch from the body's ref."""
    return _at(_branch_of_ref(r.fields().get("ref", "")))


def _ref_path(r: Request, vars_: Mapping[str, str]) -> dict[str, str]:
    """PATCH/DELETE /git/refs/{ref}: the branch from the path."""
    return _at(_branch_of_ref(vars_.get("ref", "")))


def _branch_field(name: str) -> Any:
    def extract(r: Request, vars_: Mapping[str, str]) -> dict[str, str]:
        return _at(r.fields().get(name) or None)

    return extract


def _branch_var(r: Request, vars_: Mapping[str, str]) -> dict[str, str]:
    return _at(vars_.get("branch"))


_GRAPHQL_OP = re.compile(r"^\s*(query|mutation|subscription)?\b", re.IGNORECASE)


def _graphql(r: Request, vars_: Mapping[str, str]) -> dict[str, str]:
    doc = r.json()
    query = doc.get("query", "") if isinstance(doc, dict) else ""
    if not isinstance(query, str):
        return {"_unresolved": "the GraphQL request has no query"}
    m = _GRAPHQL_OP.match(query)
    kind = (m.group(1) or "query").lower() if m else "query"
    if kind == "query":
        return {}
    return {"_unresolved": f"a GraphQL {kind} is not routed; use the REST API for writes"}


def _routes() -> list[Route]:
    rd, tr, pu, ad = "repo.read", "repo.triage", "repo.push", "repo.admin"
    out: list[Route] = [
        # Reads.
        Route("GET", "/repos/{owner}/{repo}", rd, _REPO),
        Route("GET", "/repos/{owner}/{repo}/{rest+}", rd, _REPO, spec=False),
        Route("GET", "/orgs/{org}", "org.member", _ORG),
        Route("GET", "/orgs/{org}/teams/{team_slug}", "team.member", _TEAM),
        Route("GET", "/orgs/{org}/teams/{team_slug}/{rest+}", "team.member", _TEAM, spec=False),
        Route("GET", "/orgs/{org}/{rest+}", "org.member", _ORG, spec=False),
        Route("GET", "/user", None, "", untrusted_output=False, description="the agent's own account"),
        Route("GET", "/user/{rest+}", None, "", spec=False),
        Route("GET", "/users/{username}", None, ""),
        Route("GET", "/users/{username}/{rest+}", None, "", spec=False),
        Route("GET", "/search/{rest+}", None, "", spec=False, description="search; results are untrusted"),
        Route("GET", "/rate_limit", None, "", untrusted_output=False),
        Route("GET", "/meta", None, "", untrusted_output=False),
        Route("GET", "/emojis", None, "", untrusted_output=False),
        Route("GET", "/zen", None, "", untrusted_output=False),
        Route("GET", "/octocat", None, "", untrusted_output=False),
        Route("GET", "/app", None, "", untrusted_output=False),
        Route("GET", "/app/{rest+}", None, "", untrusted_output=False, spec=False),
        Route("GET", "/installation/repositories", None, "", untrusted_output=False),
        Route("POST", "/app/installations/{installation_id}/access_tokens", None, "", untrusted_output=False, description="mint an installation token"),
        Route("GET", "/notifications", None, ""),
        Route("GET", "/notifications/{rest+}", None, "", spec=False),
        Route("GET", "/gists/{rest+}", None, "", spec=False),
        Route("GET", "/teams/{team_id}/{rest+}", None, "", spec=False, description="legacy team by numeric id: not checkable"),
        Route("GET", "/repositories/{repository_id}", None, "", description="repository by numeric id: not checkable", spec=False),
        Route("GET", "/repositories/{repository_id}/{rest+}", None, "", spec=False),
        Route("POST", "/graphql", None, "", effect="read", body=_graphql, description="GraphQL queries; mutations are refused", spec=False),
        Route("POST", "/markdown", None, "", untrusted_output=False),
        Route("POST", "/markdown/raw", None, "", untrusted_output=False),
        # Issues and pull requests.
        Route("POST", "/repos/{owner}/{repo}/issues", "issue.create", _REPO),
        Route("POST", "/repos/{owner}/{repo}/pulls", "pr.create", _REPO),
        Route("PUT", "/repos/{owner}/{repo}/pulls/{pull_number}/merge", "pr.merge", _REPO),
        Route("PUT", "/repos/{owner}/{repo}/pulls/{pull_number}/merge-async", "pr.merge", _REPO),
        Route("POST", "/repos/{owner}/{repo}/forks", rd, _REPO),
        Route("POST", "/repos/{owner}/{repo}/codespaces", rd, _REPO),
        Route("POST", "/repos/{owner}/{repo}/pulls/{pull_number}/codespaces", rd, _REPO),
        Route("PUT", "/repos/{owner}/{repo}/subscription", rd, _REPO),
        Route("DELETE", "/repos/{owner}/{repo}/subscription", rd, _REPO, effect="write"),
        Route("PUT", "/repos/{owner}/{repo}/notifications", rd, _REPO),
        Route("POST", "/repos/{template_owner}/{template_repo}/generate", rd, "repo:{template_owner}/{template_repo}"),
    ]
    # Comments, reviews and reactions publish text: pull access, sends out.
    for method, path in (
        ("POST", "/repos/{owner}/{repo}/issues/{issue_number}/comments"),
        ("POST", "/repos/{owner}/{repo}/pulls/{pull_number}/comments"),
        ("POST", "/repos/{owner}/{repo}/pulls/{pull_number}/comments/{comment_id}/replies"),
        ("POST", "/repos/{owner}/{repo}/pulls/{pull_number}/reviews"),
        ("PUT", "/repos/{owner}/{repo}/pulls/{pull_number}/reviews/{review_id}"),
        ("POST", "/repos/{owner}/{repo}/pulls/{pull_number}/reviews/{review_id}/events"),
        ("POST", "/repos/{owner}/{repo}/commits/{commit_sha}/comments"),
        ("PATCH", "/repos/{owner}/{repo}/issues/comments/{comment_id}"),
        ("PATCH", "/repos/{owner}/{repo}/pulls/comments/{comment_id}"),
        ("PATCH", "/repos/{owner}/{repo}/comments/{comment_id}"),
        ("POST", "/repos/{owner}/{repo}/issues/{issue_number}/reactions"),
        ("POST", "/repos/{owner}/{repo}/issues/comments/{comment_id}/reactions"),
        ("POST", "/repos/{owner}/{repo}/pulls/comments/{comment_id}/reactions"),
        ("POST", "/repos/{owner}/{repo}/comments/{comment_id}/reactions"),
        ("POST", "/repos/{owner}/{repo}/releases/{release_id}/reactions"),
    ):
        out.append(Route(method, path, rd, _REPO, sends_out=True))
    for path in (
        "/repos/{owner}/{repo}/issues/{issue_number}/reactions/{reaction_id}",
        "/repos/{owner}/{repo}/issues/comments/{comment_id}/reactions/{reaction_id}",
        "/repos/{owner}/{repo}/pulls/comments/{comment_id}/reactions/{reaction_id}",
        "/repos/{owner}/{repo}/comments/{comment_id}/reactions/{reaction_id}",
        "/repos/{owner}/{repo}/releases/{release_id}/reactions/{reaction_id}",
    ):
        out.append(Route("DELETE", path, rd, _REPO))
    # Triage: issue and pull request management without write access.
    for method, path in (
        ("PATCH", "/repos/{owner}/{repo}/issues/{issue_number}"),
        ("PATCH", "/repos/{owner}/{repo}/pulls/{pull_number}"),
        ("POST", "/repos/{owner}/{repo}/issues/{issue_number}/assignees"),
        ("DELETE", "/repos/{owner}/{repo}/issues/{issue_number}/assignees"),
        ("POST", "/repos/{owner}/{repo}/issues/{issue_number}/labels"),
        ("PUT", "/repos/{owner}/{repo}/issues/{issue_number}/labels"),
        ("DELETE", "/repos/{owner}/{repo}/issues/{issue_number}/labels"),
        ("DELETE", "/repos/{owner}/{repo}/issues/{issue_number}/labels/{name}"),
        ("PUT", "/repos/{owner}/{repo}/issues/{issue_number}/lock"),
        ("DELETE", "/repos/{owner}/{repo}/issues/{issue_number}/lock"),
        ("POST", "/repos/{owner}/{repo}/issues/{issue_number}/sub_issues"),
        ("DELETE", "/repos/{owner}/{repo}/issues/{issue_number}/sub_issue"),
        ("PATCH", "/repos/{owner}/{repo}/issues/{issue_number}/sub_issues/priority"),
        ("POST", "/repos/{owner}/{repo}/issues/{issue_number}/dependencies/blocked_by"),
        ("DELETE", "/repos/{owner}/{repo}/issues/{issue_number}/dependencies/blocked_by/{issue_id}"),
        ("POST", "/repos/{owner}/{repo}/issues/{issue_number}/relates_to"),
        ("DELETE", "/repos/{owner}/{repo}/issues/{issue_number}/relates_to/{issue_id}"),
        ("POST", "/repos/{owner}/{repo}/issues/{issue_number}/issue-field-values"),
        ("PUT", "/repos/{owner}/{repo}/issues/{issue_number}/issue-field-values"),
        ("DELETE", "/repos/{owner}/{repo}/issues/{issue_number}/issue-field-values/{issue_field_id}"),
        ("POST", "/repos/{owner}/{repo}/issues/{issue_number}/suggestions/{suggestion_id}/approve"),
        ("POST", "/repos/{owner}/{repo}/issues/{issue_number}/suggestions/{suggestion_id}/dismiss"),
        ("PUT", "/repos/{owner}/{repo}/issues/comments/{comment_id}/pin"),
        ("DELETE", "/repos/{owner}/{repo}/issues/comments/{comment_id}/pin"),
        ("DELETE", "/repos/{owner}/{repo}/issues/comments/{comment_id}"),
        ("DELETE", "/repos/{owner}/{repo}/pulls/comments/{comment_id}"),
        ("DELETE", "/repos/{owner}/{repo}/comments/{comment_id}"),
        ("DELETE", "/repos/{owner}/{repo}/pulls/{pull_number}/reviews/{review_id}"),
        ("POST", "/repos/{owner}/{repo}/labels"),
        ("PATCH", "/repos/{owner}/{repo}/labels/{name}"),
        ("DELETE", "/repos/{owner}/{repo}/labels/{name}"),
        ("POST", "/repos/{owner}/{repo}/milestones"),
        ("PATCH", "/repos/{owner}/{repo}/milestones/{milestone_number}"),
        ("DELETE", "/repos/{owner}/{repo}/milestones/{milestone_number}"),
        ("POST", "/repos/{owner}/{repo}/pulls/{pull_number}/requested_reviewers"),
        ("DELETE", "/repos/{owner}/{repo}/pulls/{pull_number}/requested_reviewers"),
        ("POST", "/repos/{owner}/{repo}/pulls/{pull_number}/requested_reviewers/rerequest"),
    ):
        out.append(Route(method, path, tr, _REPO))
    # Push: the repository's contents, refs, releases, statuses and runs.
    out += [
        Route("PUT", "/repos/{owner}/{repo}/contents/{path+}", pu, _REPO_AT, body=_branch_field("branch")),
        Route("DELETE", "/repos/{owner}/{repo}/contents/{path+}", pu, _REPO_AT, body=_branch_field("branch")),
        Route("POST", "/repos/{owner}/{repo}/git/refs", pu, _REPO_AT, body=_ref_body),
        Route("PATCH", "/repos/{owner}/{repo}/git/refs/{ref+}", pu, _REPO_AT, body=_ref_path),
        Route("DELETE", "/repos/{owner}/{repo}/git/refs/{ref+}", pu, _REPO_AT, body=_ref_path),
        Route("POST", "/repos/{owner}/{repo}/merges", pu, _REPO_AT, body=_branch_field("base")),
        Route("POST", "/repos/{owner}/{repo}/branches/{branch}/rename", pu, _REPO_AT, body=_branch_var),
    ]
    for method, path in (
        ("POST", "/repos/{owner}/{repo}/git/blobs"),
        ("POST", "/repos/{owner}/{repo}/git/commits"),
        ("POST", "/repos/{owner}/{repo}/git/tags"),
        ("POST", "/repos/{owner}/{repo}/git/trees"),
        ("POST", "/repos/{owner}/{repo}/merge-upstream"),
        ("POST", "/repos/{owner}/{repo}/releases"),
        ("POST", "/repos/{owner}/{repo}/releases/generate-notes"),
        ("PATCH", "/repos/{owner}/{repo}/releases/{release_id}"),
        ("DELETE", "/repos/{owner}/{repo}/releases/{release_id}"),
        ("POST", "/repos/{owner}/{repo}/releases/{release_id}/assets"),
        ("PATCH", "/repos/{owner}/{repo}/releases/assets/{asset_id}"),
        ("DELETE", "/repos/{owner}/{repo}/releases/assets/{asset_id}"),
        ("PUT", "/repos/{owner}/{repo}/pulls/{pull_number}/update-branch"),
        ("PUT", "/repos/{owner}/{repo}/pulls/{pull_number}/reviews/{review_id}/dismissals"),
        ("POST", "/repos/{owner}/{repo}/statuses/{sha}"),
        ("POST", "/repos/{owner}/{repo}/check-runs"),
        ("PATCH", "/repos/{owner}/{repo}/check-runs/{check_run_id}"),
        ("POST", "/repos/{owner}/{repo}/check-runs/{check_run_id}/rerequest"),
        ("POST", "/repos/{owner}/{repo}/check-suites"),
        ("PATCH", "/repos/{owner}/{repo}/check-suites/preferences"),
        ("POST", "/repos/{owner}/{repo}/check-suites/{check_suite_id}/rerequest"),
        ("POST", "/repos/{owner}/{repo}/dispatches"),
        ("POST", "/repos/{owner}/{repo}/actions/workflows/{workflow_id}/dispatches"),
        ("PUT", "/repos/{owner}/{repo}/actions/workflows/{workflow_id}/enable"),
        ("PUT", "/repos/{owner}/{repo}/actions/workflows/{workflow_id}/disable"),
        ("POST", "/repos/{owner}/{repo}/actions/runs/{run_id}/rerun"),
        ("POST", "/repos/{owner}/{repo}/actions/runs/{run_id}/rerun-failed-jobs"),
        ("POST", "/repos/{owner}/{repo}/actions/runs/{run_id}/cancel"),
        ("POST", "/repos/{owner}/{repo}/actions/runs/{run_id}/force-cancel"),
        ("POST", "/repos/{owner}/{repo}/actions/runs/{run_id}/approve"),
        ("POST", "/repos/{owner}/{repo}/actions/runs/{run_id}/pending_deployments"),
        ("POST", "/repos/{owner}/{repo}/actions/runs/{run_id}/deployment_protection_rule"),
        ("DELETE", "/repos/{owner}/{repo}/actions/runs/{run_id}"),
        ("DELETE", "/repos/{owner}/{repo}/actions/runs/{run_id}/logs"),
        ("POST", "/repos/{owner}/{repo}/actions/jobs/{job_id}/rerun"),
        ("DELETE", "/repos/{owner}/{repo}/actions/artifacts/{artifact_id}"),
        ("DELETE", "/repos/{owner}/{repo}/actions/caches"),
        ("DELETE", "/repos/{owner}/{repo}/actions/caches/{cache_id}"),
        ("POST", "/repos/{owner}/{repo}/deployments"),
        ("DELETE", "/repos/{owner}/{repo}/deployments/{deployment_id}"),
        ("POST", "/repos/{owner}/{repo}/deployments/{deployment_id}/statuses"),
        ("POST", "/repos/{owner}/{repo}/attestations"),
        ("POST", "/repos/{owner}/{repo}/dependency-graph/snapshots"),
        ("POST", "/repos/{owner}/{repo}/code-scanning/sarifs"),
        ("PATCH", "/repos/{owner}/{repo}/code-scanning/alerts/{alert_number}"),
        ("POST", "/repos/{owner}/{repo}/code-scanning/alerts/{alert_number}/autofix"),
        ("POST", "/repos/{owner}/{repo}/code-scanning/alerts/{alert_number}/autofix/commits"),
        ("PATCH", "/repos/{owner}/{repo}/dependabot/alerts/{alert_number}"),
        ("PATCH", "/repos/{owner}/{repo}/secret-scanning/alerts/{alert_number}"),
        ("POST", "/repos/{owner}/{repo}/stacks"),
        ("POST", "/repos/{owner}/{repo}/stacks/{stack_number}/add"),
        ("POST", "/repos/{owner}/{repo}/stacks/{stack_number}/unstack"),
        ("POST", "/repos/{owner}/{repo}/pages/builds"),
        ("POST", "/repos/{owner}/{repo}/pages/deployments"),
        ("POST", "/repos/{owner}/{repo}/pages/deployments/{pages_deployment_id}/cancel"),
    ):
        out.append(Route(method, path, pu, _REPO))
    # Admin: settings, protection, hooks, access and secrets.
    out += [
        Route("PATCH", "/repos/{owner}/{repo}", ad, _REPO),
        Route("DELETE", "/repos/{owner}/{repo}", ad, _REPO),
        Route("PUT", "/repos/{owner}/{repo}/branches/{branch}/protection", ad, _REPO_AT, body=_branch_var),
        Route("DELETE", "/repos/{owner}/{repo}/branches/{branch}/protection", ad, _REPO_AT, body=_branch_var),
        Route("*", "/repos/{owner}/{repo}/branches/{branch}/protection/{rest+}", ad, _REPO_AT, body=_branch_var, spec=False),
    ]
    for method, path in (
        ("POST", "/repos/{owner}/{repo}/hooks"),
        ("*", "/repos/{owner}/{repo}/hooks/{rest+}"),
        ("PUT", "/repos/{owner}/{repo}/collaborators/{username}"),
        ("DELETE", "/repos/{owner}/{repo}/collaborators/{username}"),
        ("PATCH", "/repos/{owner}/{repo}/invitations/{invitation_id}"),
        ("DELETE", "/repos/{owner}/{repo}/invitations/{invitation_id}"),
        ("POST", "/repos/{owner}/{repo}/keys"),
        ("DELETE", "/repos/{owner}/{repo}/keys/{key_id}"),
        ("POST", "/repos/{owner}/{repo}/rulesets"),
        ("*", "/repos/{owner}/{repo}/rulesets/{rest+}"),
        ("PUT", "/repos/{owner}/{repo}/topics"),
        ("POST", "/repos/{owner}/{repo}/transfer"),
        ("PATCH", "/repos/{owner}/{repo}/properties/values"),
        ("POST", "/repos/{owner}/{repo}/autolinks"),
        ("DELETE", "/repos/{owner}/{repo}/autolinks/{autolink_id}"),
        ("*", "/repos/{owner}/{repo}/actions/secrets/{rest+}"),
        ("POST", "/repos/{owner}/{repo}/actions/variables"),
        ("*", "/repos/{owner}/{repo}/actions/variables/{rest+}"),
        ("PUT", "/repos/{owner}/{repo}/actions/permissions"),
        ("*", "/repos/{owner}/{repo}/actions/permissions/{rest+}"),
        ("POST", "/repos/{owner}/{repo}/actions/policies"),
        ("*", "/repos/{owner}/{repo}/actions/policies/{rest+}"),
        ("*", "/repos/{owner}/{repo}/actions/runners/{rest+}"),
        ("*", "/repos/{owner}/{repo}/actions/oidc/{rest+}"),
        ("*", "/repos/{owner}/{repo}/actions/cache/{rest+}"),
        ("*", "/repos/{owner}/{repo}/agents/{rest+}"),
        ("*", "/repos/{owner}/{repo}/codespaces/secrets/{rest+}"),
        ("*", "/repos/{owner}/{repo}/dependabot/secrets/{rest+}"),
        ("PUT", "/repos/{owner}/{repo}/environments/{environment_name}"),
        ("DELETE", "/repos/{owner}/{repo}/environments/{environment_name}"),
        ("*", "/repos/{owner}/{repo}/environments/{environment_name}/{rest+}"),
        ("PUT", "/repos/{owner}/{repo}/interaction-limits"),
        ("DELETE", "/repos/{owner}/{repo}/interaction-limits"),
        ("*", "/repos/{owner}/{repo}/interaction-limits/{rest+}"),
        ("POST", "/repos/{owner}/{repo}/pages"),
        ("PUT", "/repos/{owner}/{repo}/pages"),
        ("DELETE", "/repos/{owner}/{repo}/pages"),
        ("PUT", "/repos/{owner}/{repo}/private-vulnerability-reporting"),
        ("DELETE", "/repos/{owner}/{repo}/private-vulnerability-reporting"),
        ("PUT", "/repos/{owner}/{repo}/vulnerability-alerts"),
        ("DELETE", "/repos/{owner}/{repo}/vulnerability-alerts"),
        ("PUT", "/repos/{owner}/{repo}/automated-security-fixes"),
        ("DELETE", "/repos/{owner}/{repo}/automated-security-fixes"),
        ("PUT", "/repos/{owner}/{repo}/immutable-releases"),
        ("DELETE", "/repos/{owner}/{repo}/immutable-releases"),
        ("*", "/repos/{owner}/{repo}/import"),
        ("*", "/repos/{owner}/{repo}/import/{rest+}"),
        ("PATCH", "/repos/{owner}/{repo}/code-quality/setup"),
        ("PATCH", "/repos/{owner}/{repo}/code-scanning/ai-scan"),
        ("PATCH", "/repos/{owner}/{repo}/code-scanning/default-setup"),
        ("DELETE", "/repos/{owner}/{repo}/code-scanning/analyses/{analysis_id}"),
        ("DELETE", "/repos/{owner}/{repo}/code-scanning/codeql/databases/{language}"),
        ("POST", "/repos/{owner}/{repo}/code-scanning/codeql/variant-analyses"),
        ("*", "/repos/{owner}/{repo}/secret-scanning/custom-patterns"),
        ("*", "/repos/{owner}/{repo}/secret-scanning/custom-patterns/{rest+}"),
        ("POST", "/repos/{owner}/{repo}/secret-scanning/push-protection-bypasses"),
        ("POST", "/repos/{owner}/{repo}/security-advisories"),
        ("POST", "/repos/{owner}/{repo}/security-advisories/reports"),
        ("*", "/repos/{owner}/{repo}/security-advisories/{ghsa_id}"),
        ("*", "/repos/{owner}/{repo}/security-advisories/{ghsa_id}/{rest+}"),
    ):
        out.append(Route(method, path, ad, _REPO, spec=not path.endswith("+}") and method != "*"))
    # Organizations and teams.
    out += [
        Route("POST", "/orgs/{org}/repos", "org.repo.create", _ORG),
        Route("POST", "/orgs/{org}/teams", "org.admin", _ORG),
        Route("PATCH", "/orgs/{org}/teams/{team_slug}", "team.maintainer", _TEAM),
        Route("DELETE", "/orgs/{org}/teams/{team_slug}", "org.admin", _TEAM),
        Route("*", "/orgs/{org}/teams/{team_slug}/{rest+}", "team.maintainer", _TEAM, spec=False),
        Route("PATCH", "/orgs/{org}", "org.admin", _ORG),
        Route("DELETE", "/orgs/{org}", "org.admin", _ORG),
        Route("*", "/orgs/{org}/{rest+}", "org.admin", _ORG, spec=False, description="any other change to the organization"),
        Route("POST", "/user/repos", None, "", description="a repository under the agent's own account"),
    ]
    return out


def _locate(settings: Any) -> Sequence[tuple[str, str]]:
    """api.github.com, or <url>/api/v3 (REST) and <url>/api (GraphQL) on
    GitHub Enterprise Server. Anything else on that host is not GitHub's API."""
    from toolpass.core.routes import endpoint_of_url

    url = settings.get("url") if hasattr(settings, "get") else ""
    if not url:
        return [("api.github.com", "")]
    ep = endpoint_of_url(url)
    if ep is None:
        return []
    host, path = ep
    return [(host, path + "/api/v3"), (host, path + "/api")]


ROUTES = RouteTable(
    "github",
    _routes(),
    claims=("/repos/{owner}/{repo}", "/orgs/{org}"),
    unrouted={
        ("POST", "/repos/{owner}/{repo}/actions/runners/generate-jitconfig"): "runner registration is an administrator's job, done outside agents",
        ("*", "/orgs/{org}/{security_product}/{enablement}"): "covered by the organization catch-all",
    },
    locate=_locate,
)

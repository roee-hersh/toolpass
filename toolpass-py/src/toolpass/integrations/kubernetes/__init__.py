"""Checks permissions with SubjectAccessReview.

toolpass authenticates to the API server with a ServiceAccount token whose
only permission is `create subjectaccessreviews.authorization.k8s.io`. It
derives the Kubernetes username from the caller's email with a template,
adds the caller's groups (with an optional prefix) and system:authenticated,
and asks the API server whether that subject may perform the verb on the
resource. The API server evaluates RBAC and every configured authorizer;
nothing is persisted.

Kubernetes has no user directory, so when the caller sends no groups a
groups_connection (an aws connection for EKS, a directory such as Google
Workspace, Microsoft 365 or Okta) can name them from the email instead.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

from toolpass.core import integration as integ
from toolpass.core import jsonx
from toolpass.core.cache import TTL, is_panic_type
from toolpass.core.catalog import Action, go_bytes, go_decode
from toolpass.core.context import Context
from toolpass.core.decision import Code, Decision, allowed, denied, errorf, unsupported, user_not_found, wrap_error
from toolpass.core.errors import go_quote, go_trim_space
from toolpass.core.routes import RouteTable
from toolpass.core.template import Template, parse_template, validate_template
from toolpass.net import httpx

from .actions import ALIAS_LIST, Attributes, build_attributes, parse_raw

__all__ = ["INTEGRATION", "Connection", "Integration", "NotFoundError"]

T = TypeVar("T")

# Applies when username_template is unset.
DEFAULT_TEMPLATE = "{email}"

SAR_PATH = "/apis/authorization.k8s.io/v1/subjectaccessreviews"
SELF_RULES_PATH = "/apis/authorization.k8s.io/v1/selfsubjectrulesreviews"

# Marks the groups the API server assigns itself (system:masters,
# system:authenticated, system:nodes, ...).
RESERVED_GROUP_PREFIX = "system:"

AWS_AUTH_PATH = "/api/v1/namespaces/kube-system/configmaps/aws-auth"
# How long the aws-auth ConfigMap is reused across users.
AWS_AUTH_TTL = 60.0


class Integration(integ.Integration):
    """The kubernetes product."""

    def name(self) -> str:
        return "kubernetes"

    def fields(self) -> list[integ.Field]:
        return [
            integ.url_field(True, "API server URL, e.g. https://10.20.0.5:6443"),
            integ.credential_field(True, "ServiceAccount token allowed to create subjectaccessreviews"),
            integ.Field(
                name="username_template",
                default=DEFAULT_TEMPLATE,
                validate=validate_template,
                description="how the API server names users: placeholders {email}, {local}, {domain}, e.g. oidc:{email}",
            ),
            integ.Field(name="group_prefix", description="prefix the API server puts on OIDC groups, e.g. oidc:"),
            integ.Field(
                name="add_authenticated_group",
                default="true",
                enum=("true", "false"),
                description="also send system:authenticated, as the API server would",
            ),
            integ.groups_connection_field(
                "where the user's groups come from when the caller sends none: an aws connection (EKS access entries) or a directory",
                ref=f"@{integ.CAP_KUBERNETES_SUBJECT}|@{integ.CAP_GROUPS}",
            ),
            integ.Field(
                name="eks_cluster",
                validate=_validate_eks_cluster,
                description="the EKS cluster name, required when groups_connection is an aws connection",
            ),
            integ.Field(
                name="eks_aws_auth",
                default="false",
                enum=("true", "false"),
                description="also read the kube-system/aws-auth ConfigMap (mapRoles, mapUsers) for principals with no access entry",
            ),
        ]

    def actions(self) -> list[Action]:
        acts = [
            Action(
                name="raw:<verb>:<resource>[.<group>][/<subresource>]",
                pattern=True,
                description="any Kubernetes verb on any resource, e.g. raw:create:deployments.apps, raw:get:pods/log",
            ),
            Action(
                name="raw:<verb>",
                pattern=True,
                description="a verb on a non-resource path, used with nonresource:<path>, e.g. raw:get with nonresource:/metrics",
            ),
        ]
        acts.extend(Action(name=a.name, description=a.desc) for a in ALIAS_LIST)
        return acts

    def routes(self) -> RouteTable | None:
        from toolpass.integrations.kubernetes.routes import ROUTES

        return ROUTES

    def match_action(self, name: str) -> Action | None:
        """Accept raw:<verb>:<resource> patterns."""
        if not name.startswith("raw:"):
            return None
        try:
            parse_raw(name)
        except ValueError:
            return None
        return Action(name=name, pattern=True, description="raw Kubernetes verb")

    def new(self, ctx: Context, s: integ.Settings, d: integ.Deps) -> Connection:
        hc = d.http_client(s)
        cred = s.secret("credential")
        if cred.is_zero():
            raise ValueError("credential is required")
        tpl_text = s.get("username_template")
        if tpl_text == "":
            tpl_text = DEFAULT_TEMPLATE
        try:
            tpl = parse_template(tpl_text)
        except ValueError as e:
            raise ValueError(f"username_template: {e}") from e
        client = httpx.Client(
            http=hc,
            base=s.get("url"),
            logger=d.logger,
            auth=httpx.bearer_auth(lambda _ctx: cred.get_string()),
        )
        c = Connection(s, client, tpl, s.get("group_prefix"), s.bool("add_authenticated_group", True))
        ref = s.get("groups_connection")
        c.eks_cluster = s.get("eks_cluster")
        c.eks_aws_auth = s.bool("eks_aws_auth", False)
        c.aws_auth_cache.set_clock(d.now)
        if ref != "":
            conn = d.connection(ref)
            if isinstance(conn, integ.KubernetesSubjectSource):
                c.groups_source = conn
            elif isinstance(conn, integ.GroupsSource):
                c.directory = conn
            else:
                raise ValueError(f"groups_connection {go_quote(ref)} cannot look up Kubernetes groups")
            needs_cluster = c.groups_source is not None and bool(c.groups_source.kubernetes_needs_cluster)
            if needs_cluster and c.eks_cluster == "":
                raise ValueError("eks_cluster is required when groups_connection is an aws connection")
            if not needs_cluster and c.eks_cluster != "":
                raise ValueError("eks_cluster is used only when groups_connection is an aws connection")
            if not needs_cluster and c.eks_aws_auth:
                raise ValueError("eks_aws_auth is used only when groups_connection is an aws connection")
        elif c.eks_cluster != "" or c.eks_aws_auth:
            raise ValueError("eks_cluster and eks_aws_auth need groups_connection set to an aws connection")
        return c


def _validate_eks_cluster(v: str) -> None:
    from toolpass.integrations.aws.eks import validate_eks_cluster

    validate_eks_cluster(v)


class NotFoundError(Exception):
    """Raised by Connection.get for a 404 (Go: kubernetes.ErrNotFound)."""

    def __init__(self) -> None:
        super().__init__("kubernetes: not found")


def _fail(err: Exception) -> None:
    """Re-raise a crash (Go: a panic) as it is; everything else is an error."""
    if is_panic_type(err):
        raise err


class Connection(integ.Connection):
    """One cluster."""

    def __init__(self, settings: integ.Settings, client: httpx.Client, template: Template, prefix: str, add_auth: bool) -> None:
        self.settings = settings
        self.client = client
        self.template = template
        self.prefix = prefix
        self.add_auth = add_auth
        # One of: a source of Kubernetes subjects (EKS access entries, GKE's
        # security group) or a plain directory whose groups are treated like
        # groups a caller sent.
        self.groups_source: integ.KubernetesSubjectSource | None = None
        self.directory: integ.GroupsSource | None = None
        self.eks_cluster = ""
        self.eks_aws_auth = False
        self.aws_auth_cache: TTL[str, dict[str, Any] | None] = TTL(1)

    def get(self, ctx: Context, path: str, decode: Callable[[Any], T] | None = None) -> T:
        """GET against the API server and decode the JSON response with
        decode (the identity when None). Other integrations that live
        inside a cluster (argocd) read their configuration through it. A
        404 raises NotFoundError; 401/403, transport and decode failures
        raise ToolpassError."""
        try:
            _, v = self.client.get_json(ctx, path)
            return decode(v) if decode is not None else v
        except Exception as err:
            _fail(err)
            st = httpx.status(err)
            if st == 404:
                raise NotFoundError() from None
            if st == 403:
                raise wrap_error(Code.CREDENTIAL_REJECTED, err, f"toolpass's ServiceAccount may not read {path} (HTTP 403)") from err
            raise httpx.classify(err) from err  # type: ignore[misc]

    def username(self, email: str) -> str:
        """Apply the template to an email."""
        return self.template.render(email)

    def resolve_identity(self, ctx: Context, u: integ.User) -> integ.Identity:
        """A string transform of the email; the groups are the caller's or,
        when the caller sends none, groups_connection's."""
        name = self.username(u.email)
        groups: list[str] = []
        if not u.groups and self.directory is not None:
            groups.extend(self._prefixed(integ.claimed_groups(self.directory, ctx, u), " from groups_connection"))
        elif not u.groups and self.groups_source is not None:
            subject = self._subject(ctx, u.email)
            if subject.username != "":
                name = subject.username
            if subject.mapped:
                # Kubernetes group names chosen by the cluster's admin (an
                # EKS access entry), sent as they are: no prefix, and
                # system:masters is legitimate here.
                groups.extend(subject.groups)
            else:
                groups.extend(self._prefixed(subject.groups, " from groups_connection"))
        else:
            groups.extend(self._prefixed(u.groups, ""))
        if name.startswith(RESERVED_GROUP_PREFIX):
            # system:serviceaccount:..., system:kube-controller-manager and
            # the like are Kubernetes' own identities; an email must never
            # become one through the template or a mapping.
            raise errorf(Code.INVALID_REQUEST, f"user {go_quote(u.email)} maps to the reserved Kubernetes username {go_quote(name)}")
        if self.add_auth and "system:authenticated" not in groups:
            groups.append("system:authenticated")
        out: list[str] = []
        for g in groups:
            if g not in out:
                out.append(g)
        return integ.Identity(id=name, display=name, groups=tuple(out))

    def _prefixed(self, groups: tuple[str, ...], source: str) -> list[str]:
        out: list[str] = []
        for g in groups:
            if self.prefix == "" and g.startswith(RESERVED_GROUP_PREFIX):
                # Without a prefix the value reaches the review as is, and
                # system:masters is cluster-admin on most clusters.
                raise errorf(
                    Code.INVALID_REQUEST,
                    f"group {go_quote(g)}{source} is reserved by Kubernetes; toolpass only sends {RESERVED_GROUP_PREFIX} groups it adds itself "
                    "unless the connection sets group_prefix",
                )
            out.append(self.prefix + g)
        return out

    def _subject(self, ctx: Context, email: str) -> integ.KubernetesSubject:
        """Ask groups_connection, then (eks_aws_auth) the aws-auth ConfigMap
        for each principal that has no access entry, as EKS falls back to
        aws-auth per principal on an API_AND_CONFIG_MAP cluster."""
        assert self.groups_source is not None
        subject = self.groups_source.kubernetes_subject(ctx, email, self.eks_cluster)
        tried = ", ".join(subject.tried) or "none"
        if self.eks_aws_auth and subject.tried:
            from toolpass.integrations.aws.eks import match_aws_auth, merge_mappings

            data = self._aws_auth(ctx)
            try:
                extra = match_aws_auth(data, subject.tried) if data is not None else []
            except ValueError as e:
                raise errorf(Code.UPSTREAM_ERROR, str(e)) from None
            if extra:
                username, groups = merge_mappings(extra, email)
                if subject.found:
                    # Access entries and aws-auth rows together: more than one
                    # mapping, so no single username applies.
                    username = ""
                    groups = tuple(dict.fromkeys((*subject.groups, *groups)))
                rest = tuple(p for p in subject.tried if p not in {m.principal for m in extra})
                return integ.KubernetesSubject(username=username, groups=groups, mapped=True, tried=rest)
            if not subject.found:
                raise user_not_found(f"{email} has no access entry on EKS cluster {self.eks_cluster} and no aws-auth mapping; tried {tried}")
        if subject.found:
            return subject
        raise user_not_found(f"{email} has no access entry on EKS cluster {self.eks_cluster}; tried {tried}")

    def _aws_auth(self, ctx: Context) -> dict[str, Any] | None:
        """The aws-auth ConfigMap's data, reused for a minute across users;
        None when the cluster has none."""
        return self.aws_auth_cache.do(ctx, "aws-auth", lambda ctx: (self._read_aws_auth(ctx), AWS_AUTH_TTL))

    def _read_aws_auth(self, ctx: Context) -> dict[str, Any] | None:
        try:
            cm: Any = self.get(ctx, AWS_AUTH_PATH)
        except NotFoundError:
            return None
        try:
            return jsonx.o(jsonx.obj(cm), "data")
        except ValueError as e:
            raise errorf(Code.UPSTREAM_ERROR, f"the aws-auth ConfigMap is unreadable: {e}") from None

    def check(self, ctx: Context, r: integ.CheckRequest) -> Decision:
        """Post one SubjectAccessReview."""
        try:
            attrs = build_attributes(r.action_name, r.resource)
        except ValueError as e:
            raise errorf(Code.INVALID_REQUEST, str(e)) from None
        spec: dict[str, Any] = {"user": r.identity.id}
        if r.identity.groups:
            spec["groups"] = list(r.identity.groups)
        if attrs.non_resource_path != "":
            spec["nonResourceAttributes"] = {"path": attrs.non_resource_path, "verb": attrs.verb}
        else:
            spec["resourceAttributes"] = _resource_attributes(attrs)
        req = {"apiVersion": "authorization.k8s.io/v1", "kind": "SubjectAccessReview", "spec": spec}
        try:
            _, out = self.client.post_json(ctx, SAR_PATH, req, idempotent=True)
            st = _sar_status(out)
        except Exception as err:
            _fail(err)
            if httpx.status(err) == 403:
                raise wrap_error(Code.CREDENTIAL_REJECTED, err, "toolpass's ServiceAccount may not create subjectaccessreviews (HTTP 403)") from err
            raise httpx.classify(err) from err  # type: ignore[misc]
        what = describe(attrs)
        if jsonx.b(st, "allowed"):
            return allowed(f"{r.identity.id} may {what}{suffix(jsonx.s(st, 'reason'))}")
        evaluation_error = jsonx.s(st, "evaluationError")
        if evaluation_error != "":
            return unsupported(f"the API server could not evaluate {what}: {evaluation_error}")
        return denied(f"{r.identity.id} may not {what}{suffix(jsonx.s(st, 'reason'))}")

    def probe(self, ctx: Context) -> integ.ProbeResult:
        """Post a review for a throwaway subject. Any 201 proves the token
        is valid and may create reviews. It reveals nothing about real users."""
        req = {
            "apiVersion": "authorization.k8s.io/v1",
            "kind": "SubjectAccessReview",
            "spec": {"user": "toolpass:probe", "resourceAttributes": {"verb": "get", "resource": "namespaces", "name": "default"}},
        }
        try:
            _, out = self.client.post_json(ctx, SAR_PATH, req, idempotent=True)
            st = _sar_status(out)
        except Exception as err:
            _fail(err)
            if httpx.status(err) == 403:
                raise RuntimeError(
                    "the token is valid but may not create subjectaccessreviews.authorization.k8s.io; grant a ClusterRole with that one rule"
                ) from None
            raise httpx.classify(err) from err  # type: ignore[misc]
        warnings: list[str] = []
        if jsonx.b(st, "allowed"):
            warnings.append("the cluster lets an unknown user read namespaces; check that authorization is enabled")
        warnings.extend(self._extra_rules(ctx))
        if self.eks_aws_auth:
            try:
                if self._aws_auth(ctx) is None:
                    warnings.append("eks_aws_auth is set but the cluster has no kube-system/aws-auth ConfigMap")
            except Exception as err:  # noqa: BLE001 - _fail re-raises a crash; any failure is a warning
                _fail(err)
                warnings.append(f"eks_aws_auth is set but the aws-auth ConfigMap could not be read: {err}")
        return integ.ProbeResult(summary="can create SubjectAccessReviews", warnings=tuple(warnings))

    def _extra_rules(self, ctx: Context) -> list[str]:
        """Ask the API server what toolpass's own token may do (any
        authenticated subject may create a SelfSubjectRulesReview) and warn
        about anything beyond creating SubjectAccessReviews. Rules every
        authenticated user has (self reviews, discovery) are ignored."""
        req = {"apiVersion": "authorization.k8s.io/v1", "kind": "SelfSubjectRulesReview", "spec": {"namespace": "kube-system"}}
        try:
            _, out = self.client.post_json(ctx, SELF_RULES_PATH, req, idempotent=True)
            rules = _resource_rules(out)
        except Exception as err:  # noqa: BLE001 - _fail re-raises a crash; any failure is a warning
            _fail(err)
            return ["could not list the token's own permissions (SelfSubjectRulesReview failed); check for over-privilege by hand"]
        extra: list[str] = []
        for verbs, groups, resources, names in rules:
            if _all_self_review(resources):
                continue
            if resources == ["subjectaccessreviews"] and verbs == ["create"]:
                continue
            if self.eks_aws_auth and resources == ["configmaps"] and verbs == ["get"] and names == ["aws-auth"] and groups in ([], [""]):
                # The one extra rule eks_aws_auth needs.
                continue
            extra.append(",".join(verbs) + " " + ",".join(resources) + _group_suffix(groups))
        if not extra:
            return []
        if len(extra) > 8:
            extra = [*extra[:8], f"and {len(extra) - 8} more"]
        return ["the token can do more than create SubjectAccessReviews (in kube-system): " + "; ".join(extra) + ". Bind only the toolpass ClusterRole to it"]


def _resource_attributes(a: Attributes) -> dict[str, str]:
    """The resourceAttributes object, with Go's omitempty fields left out
    when empty and the keys in Go's field order."""
    out: dict[str, str] = {}
    if a.namespace:
        out["namespace"] = a.namespace
    out["verb"] = a.verb
    if a.group:
        out["group"] = a.group
    out["resource"] = a.resource
    if a.subresource:
        out["subresource"] = a.subresource
    if a.name:
        out["name"] = a.name
    return out


def _sar_status(v: Any) -> dict[str, Any]:
    """The status of a SubjectAccessReview answer, type-checked the way Go
    decodes it into sarResponse."""
    st = jsonx.o(jsonx.obj(v), "status")
    jsonx.b(st, "allowed")
    jsonx.b(st, "denied")
    jsonx.s(st, "reason")
    jsonx.s(st, "evaluationError")
    return st


def _resource_rules(v: Any) -> list[tuple[list[str], list[str], list[str], list[str]]]:
    """(verbs, apiGroups, resources, resourceNames) of each resourceRules
    entry of a SelfSubjectRulesReview answer."""
    st = jsonx.o(jsonx.obj(v), "status")
    jsonx.b(st, "incomplete")
    jsonx.s(st, "evaluationError")
    out = []
    for r in jsonx.arr(st, "resourceRules"):
        r = jsonx.obj(r)
        out.append((jsonx.strs(r, "verbs"), jsonx.strs(r, "apiGroups"), jsonx.strs(r, "resources"), jsonx.strs(r, "resourceNames")))
    return out


def suffix(reason: str) -> str:
    reason = go_trim_space(reason)
    if reason == "":
        return ""
    b = go_bytes(reason)
    if len(b) > 200:
        # Go cuts at byte 200, which may split a rune; each byte of a split
        # rune reads as U+FFFD, as Go's JSON encoder writes it.
        reason = go_decode(b[:200]) + "..."
    return " (" + reason + ")"


def describe(a: Attributes) -> str:
    if a.non_resource_path != "":
        return a.verb + " " + a.non_resource_path
    res = a.resource
    if a.group != "":
        res += "." + a.group
    if a.subresource != "":
        res += "/" + a.subresource
    s = a.verb + " " + res
    if a.name != "":
        s += " " + a.name
    if a.namespace != "":
        s += " in namespace " + a.namespace
    return s


def _all_self_review(resources: list[str]) -> bool:
    if not resources:
        return False
    return all(r in ("selfsubjectaccessreviews", "selfsubjectrulesreviews", "selfsubjectreviews") for r in resources)


def _group_suffix(groups: list[str]) -> str:
    if not groups or groups == [""]:
        return ""
    return "." + ",".join(groups)


INTEGRATION = Integration()

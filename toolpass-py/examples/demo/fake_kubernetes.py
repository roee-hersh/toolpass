"""A stand-in Kubernetes API server with namespaces shop-staging and shop-prod,
each running the shop-web Deployment.

Kubernetes has no user directory: toolpass's kubernetes integration turns a
person's email into the username the API server knows them by (here
``oidc:{email}``, as an OIDC-configured cluster names them) and posts a
SubjectAccessReview (``POST /apis/authorization.k8s.io/v1/subjectaccessreviews``)
asking whether that user may perform a verb on a resource. This fake answers
those reviews from ``K8S_RBAC`` in ``world.py``: the verbs each person may
use on Deployments, per namespace.

The demo's tools read a Deployment and patch its container image; the fake
keeps the new image in memory.
"""

from __future__ import annotations

import copy
import pathlib
import secrets
from typing import Any

from fakebase import Fake, Reply, Req
from world import K8S_DEPLOYMENTS, K8S_RBAC

SAR = "/apis/authorization.k8s.io/v1/subjectaccessreviews"
SELF_RULES = "/apis/authorization.k8s.io/v1/selfsubjectrulesreviews"
USERNAME_TEMPLATE = "oidc:{email}"
GRANTED = ("apps", "deployments")  # the (group, resource) the grants in K8S_RBAC are on


def deployment(ns: str, name: str, image: str) -> dict[str, Any]:
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": name, "namespace": ns, "generation": 1, "labels": {"app": name}},
        "spec": {
            "replicas": 2,
            "selector": {"matchLabels": {"app": name}},
            "template": {"metadata": {"labels": {"app": name}}, "spec": {"containers": [{"name": name, "image": image}]}},
        },
        "status": {"observedGeneration": 1, "replicas": 2, "updatedReplicas": 2, "readyReplicas": 2, "availableReplicas": 2},
    }


def not_found(what: str) -> Reply:
    return Reply(404, {"kind": "Status", "apiVersion": "v1", "status": "Failure", "message": f"{what} not found", "reason": "NotFound", "code": 404})


class KubernetesFake(Fake):
    name = "kubernetes"

    def __init__(self) -> None:
        super().__init__()
        self.deployments = {key: deployment(*key, image) for key, image in K8S_DEPLOYMENTS.items()}

    def handle(self, r: Req) -> Reply:
        p, m = r.path, r.method
        with self.lock:
            # What toolpass asks, with its ServiceAccount token.
            if p in (SAR, SELF_RULES) and m == "POST":
                if not r.headers.get("authorization", "").startswith("Bearer "):
                    return Reply(401, {"kind": "Status", "status": "Failure", "reason": "Unauthorized", "code": 401})
                return Reply(201, {**r.body, "status": self._review(r.body) if p == SAR else self._own_rules()})
            # What the tools do.
            parts = p.removeprefix("/apis/apps/v1/namespaces/").split("/")
            if p.startswith("/apis/apps/v1/namespaces/") and len(parts) in (2, 3) and parts[1] == "deployments":
                if len(parts) == 2 and m == "GET":
                    items = [d for (ns, _), d in self.deployments.items() if ns == parts[0]]
                    return Reply(200, {"apiVersion": "apps/v1", "kind": "DeploymentList", "items": items})
                d = self.deployments.get((parts[0], parts[2])) if len(parts) == 3 else None
                if d is None:
                    return not_found(f'deployments.apps "{parts[-1]}"')
                if m == "GET":
                    return Reply(200, d)
                if m == "PATCH":
                    return self._patch(d, r.body)
        return Reply(404, {"kind": "Status", "status": "Failure", "message": f"no route {m} {p}", "reason": "NotFound", "code": 404})

    def _review(self, body: Any) -> dict[str, Any]:
        """Allowed when the user's grants in the namespace include the verb
        on Deployments themselves (no subresources)."""
        spec = body.get("spec") or {}
        ra = spec.get("resourceAttributes") or {}
        user = spec.get("user", "")
        email = user.removeprefix("oidc:") if user.startswith("oidc:") else ""
        verbs = K8S_RBAC.get(email, {}).get(ra.get("namespace", ""), set())
        ok = (ra.get("group", ""), ra.get("resource")) == GRANTED and not ra.get("subresource") and ra.get("verb") in verbs
        what = f"{ra.get('verb')} {ra.get('resource')} in {ra.get('namespace') or 'the cluster'}"
        return {"allowed": ok, "reason": f'RBAC: allowed by RoleBinding "shop" for {user}' if ok else f"RBAC: {user} may not {what}"}

    def _own_rules(self) -> dict[str, Any]:
        """toolpass's ServiceAccount may only create SubjectAccessReviews."""
        rule = {"verbs": ["create"], "apiGroups": ["authorization.k8s.io"], "resources": ["subjectaccessreviews"]}
        return {"resourceRules": [rule], "nonResourceRules": [], "incomplete": False}

    def _patch(self, d: dict[str, Any], body: Any) -> Reply:
        """A strategic-merge or merge patch of container images, matched by name."""
        try:
            patched = (body.get("spec") or {})["template"]["spec"]["containers"]
        except (AttributeError, KeyError, TypeError):
            return Reply(422, {"kind": "Status", "status": "Failure", "message": "only spec.template.spec.containers images can be patched", "code": 422})
        containers = d["spec"]["template"]["spec"]["containers"]
        for c in patched:
            for have in containers:
                if have["name"] == c.get("name") and c.get("image"):
                    have["image"] = c["image"]
        d["metadata"]["generation"] += 1
        d["status"]["observedGeneration"] = d["metadata"]["generation"]
        return Reply(200, copy.deepcopy(d))


def connection(url: str, secrets_dir: pathlib.Path) -> dict[str, Any]:
    """The toolpass connection to the fake cluster, with a ServiceAccount
    token for toolpass (the fake accepts any bearer token)."""
    token = secrets_dir / "k8s-token"
    token.write_text(secrets.token_urlsafe(32))
    token.chmod(0o600)
    return {"id": "k8s-acme", "integration": "kubernetes", "url": url, "credential": f"file:{token}", "username_template": USERNAME_TEMPLATE}

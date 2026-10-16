"""The demo's stand-in Kubernetes API server gives toolpass's real kubernetes
integration the answers world.py implies, and the tool endpoints work."""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

DEMO = str(Path(__file__).resolve().parents[2] / "examples" / "demo")
if DEMO not in sys.path:
    sys.path.insert(0, DEMO)

from fake_kubernetes import KubernetesFake, connection  # noqa: E402
from world import DANA, K8S_DEPLOYMENTS, SAM  # noqa: E402

from toolpass import Toolpass  # noqa: E402
from toolpass._wire import Connections  # noqa: E402
from toolpass.core.decision import Code  # noqa: E402
from toolpass.core.routes import Request  # noqa: E402

CONN = "k8s-acme"
DEPLOY = "/apis/apps/v1/namespaces/{ns}/deployments/shop-web"
MERGE = "application/merge-patch+json"
STRATEGIC = "application/strategic-merge-patch+json"


@pytest.fixture
def env(tmp_path: Path) -> Iterator[tuple[KubernetesFake, Toolpass, str]]:
    fake = KubernetesFake()
    url = fake.start()
    try:
        yield fake, Toolpass(connections=[connection(url, tmp_path)]), url
    finally:
        fake.stop()


def route(tp: Toolpass, url: str, method: str, path: str, ctype: str = "application/json", body: Any = None) -> tuple[str | None, str]:
    """The connection, action and resource the wire hook would check for the request."""
    u = urllib.parse.urlsplit(url + path)
    raw = json.dumps(body).encode() if body is not None else None
    req = Request(u.scheme, u.hostname or "", u.port, method, u.path, u.query, {"content-type": ctype}, raw)
    found = Connections.from_toolpass(tp).resolve(req)
    assert found is not None
    conn, prefix = found
    assert conn.id == CONN
    o = conn.table.match(req, prefix)
    assert o is not None and o.unresolved is None, o
    return o.action, o.resource


def call(url: str, method: str, path: str, body: Any = None, ctype: str = "application/json") -> tuple[int, Any]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url + path, data, {"Content-Type": ctype, "Authorization": "Bearer tool"}, method=method)
    with urllib.request.urlopen(req) as resp:
        return resp.status, json.loads(resp.read())


def image_patch(image: str) -> dict[str, Any]:
    return {"spec": {"template": {"spec": {"containers": [{"name": "shop-web", "image": image}]}}}}


@pytest.mark.parametrize(
    ("method", "ns", "ctype", "verb", "dana", "sam"),
    [
        ("GET", "shop-staging", "application/json", "get", True, True),
        ("PATCH", "shop-staging", STRATEGIC, "patch", True, False),
        ("PATCH", "shop-staging", MERGE, "patch", True, False),
        ("PATCH", "shop-prod", STRATEGIC, "patch", False, False),
        ("GET", "shop-prod", "application/json", "get", True, True),
    ],
)
def test_deployment_checks(env: tuple[KubernetesFake, Toolpass, str], method: str, ns: str, ctype: str, verb: str, dana: bool, sam: bool) -> None:
    _, tp, url = env
    body = image_patch("ghcr.io/acme/shop-web:1.5.0") if method == "PATCH" else None
    action, resource = route(tp, url, method, DEPLOY.format(ns=ns), ctype, body)
    assert action == f"raw:{verb}:deployments.apps"
    assert resource == f"namespace:{ns}?resource=deployments.apps&name=shop-web"
    for user, want in ((DANA, dana), (SAM, sam)):
        d = tp.check(user, CONN, action, resource)
        assert d.code == (Code.ALLOWED if want else Code.DENIED), (user, d)


def test_other_resources_and_subresources_are_denied(env: tuple[KubernetesFake, Toolpass, str]) -> None:
    _, tp, url = env
    action, resource = route(tp, url, "GET", "/api/v1/namespaces/shop-staging/secrets/db")
    assert tp.check(DANA, CONN, action, resource).code == Code.DENIED
    action, resource = route(tp, url, "PATCH", DEPLOY.format(ns="shop-staging") + "/scale", MERGE, {"spec": {"replicas": 5}})
    assert action == "raw:patch:deployments.apps/scale"
    assert tp.check(DANA, CONN, action, resource).code == Code.DENIED


def test_unknown_person_is_denied(env: tuple[KubernetesFake, Toolpass, str]) -> None:
    _, tp, url = env
    action, resource = route(tp, url, "GET", DEPLOY.format(ns="shop-staging"))
    assert tp.check("nobody@acme.dev", CONN, action, resource).code == Code.DENIED


def test_probe(env: tuple[KubernetesFake, Toolpass, str]) -> None:
    _, tp, _ = env
    [(cid, ok, summary)] = tp.probe()
    assert (cid, ok) == (CONN, True), summary


def test_read_and_patch_deployment(env: tuple[KubernetesFake, Toolpass, str]) -> None:
    _, _, url = env
    status, d = call(url, "GET", DEPLOY.format(ns="shop-staging"))
    assert status == 200 and d["kind"] == "Deployment"
    assert d["spec"]["template"]["spec"]["containers"][0]["image"] == K8S_DEPLOYMENTS[("shop-staging", "shop-web")]
    assert d["status"]["readyReplicas"] == 2

    status, d = call(url, "PATCH", DEPLOY.format(ns="shop-staging"), image_patch("ghcr.io/acme/shop-web:1.5.0"), STRATEGIC)
    assert status == 200 and d["spec"]["template"]["spec"]["containers"][0]["image"] == "ghcr.io/acme/shop-web:1.5.0"
    _, again = call(url, "GET", DEPLOY.format(ns="shop-staging"))
    assert again["spec"]["template"]["spec"]["containers"][0]["image"] == "ghcr.io/acme/shop-web:1.5.0"
    assert again["metadata"]["generation"] == 2
    # The other namespace's copy is untouched.
    _, prod = call(url, "GET", DEPLOY.format(ns="shop-prod"))
    assert prod["spec"]["template"]["spec"]["containers"][0]["image"] == K8S_DEPLOYMENTS[("shop-prod", "shop-web")]

    _, listed = call(url, "GET", "/apis/apps/v1/namespaces/shop-prod/deployments")
    assert [i["metadata"]["name"] for i in listed["items"]] == ["shop-web"]


def test_missing_deployment_is_404(env: tuple[KubernetesFake, Toolpass, str]) -> None:
    _, _, url = env
    with pytest.raises(urllib.error.HTTPError) as e:
        call(url, "GET", "/apis/apps/v1/namespaces/shop-staging/deployments/nope")
    assert e.value.code == 404

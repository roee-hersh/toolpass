"""The googlecloud route table: Google Cloud REST requests mapped to IAM
permissions on full resource names, and its shape."""

from __future__ import annotations

import json

import pytest

from tests.harness.routes import check_routes_against_spec
from toolpass.core.catalog import parse_resource
from toolpass.core.integration import Settings, find_action
from toolpass.core.routes import Request
from toolpass.integrations.googlecloud import GoogleCloud
from toolpass.integrations.googlecloud.actions import parse_ref
from toolpass.integrations.googlecloud.routes import HOSTS, ROUTES

SA = "deployer@my-proj.iam.gserviceaccount.com"
BOUNDARY = "b0undary"


def req(
    method: str, host: str, path: str, query: str = "", body: object = None, *, ct: str = "application/json", headers: dict[str, str] | None = None
) -> Request:
    raw = body if isinstance(body, bytes) else (json.dumps(body).encode() if body is not None else None)
    h = {"content-type": ct} if raw is not None else {}
    h.update(headers or {})
    return Request("https", host, 443, method, path, query, h, raw)


def test_routes_match_the_api_description():
    # The troubleshooter's one method is the only Google Cloud description the tests hold.
    assert check_routes_against_spec(ROUTES, "google-policytroubleshooter", host="policytroubleshooter.googleapis.com") == []


def test_every_route_names_a_known_action():
    integration = GoogleCloud()
    for r in ROUTES.routes:
        assert r.action is None or find_action(integration, r.action) is not None, r


GCS, BQ, SM, IAM, IAMC = (
    "storage.googleapis.com",
    "bigquery.googleapis.com",
    "secretmanager.googleapis.com",
    "iam.googleapis.com",
    "iamcredentials.googleapis.com",
)
GCE, RUN, GKE, CRM = "compute.googleapis.com", "run.googleapis.com", "container.googleapis.com", "cloudresourcemanager.googleapis.com"
TBL = "/bigquery/v2/projects/my-proj/datasets/sales/tables/orders"

ROUTING = [
    # Cloud Storage.
    (
        "GET",
        GCS,
        "/storage/v1/b",
        "project=my-proj",
        None,
        "raw:storage.buckets.list",
        "project:my-proj",
        "read",
        "//cloudresourcemanager.googleapis.com/projects/my-proj",
    ),
    (
        "GET",
        GCS,
        "/storage/v1/b/acme-data",
        "",
        None,
        "raw:storage.buckets.get",
        "bucket:acme-data",
        "read",
        "//storage.googleapis.com/projects/_/buckets/acme-data",
    ),
    ("PATCH", GCS, "/storage/v1/b/acme-data", "", {"labels": {"a": "b"}}, "raw:storage.buckets.update", "bucket:acme-data", "write", None),
    ("PATCH", GCS, "/storage/v1/b/acme-data", "", {"acl": []}, "iam.set", "bucket:acme-data", "write", None),
    ("DELETE", GCS, "/storage/v1/b/acme-data", "", None, "bucket.delete", "bucket:acme-data", "destructive", None),
    ("PUT", GCS, "/storage/v1/b/acme-data/iam", "", {"bindings": []}, "iam.set", "bucket:acme-data", "write", None),
    ("GET", GCS, "/storage/v1/b/acme-data/o", "prefix=reports/", None, "storage.list", "bucket:acme-data", "read", None),
    (
        "GET",
        GCS,
        "/storage/v1/b/acme-data/o/reports%2Fq1.csv",
        "alt=media",
        None,
        "storage.read",
        "object:acme-data/reports/q1.csv",
        "read",
        "//storage.googleapis.com/projects/_/buckets/acme-data/objects/reports/q1.csv",
    ),
    ("DELETE", GCS, "/storage/v1/b/acme-data/o/reports%2Fq1.csv", "", None, "storage.delete", "object:acme-data/reports/q1.csv", "destructive", None),
    ("PATCH", GCS, "/storage/v1/b/acme-data/o/a.txt", "", {"metadata": {"k": "v"}}, "raw:storage.objects.update", "object:acme-data/a.txt", "write", None),
    (
        "POST",
        GCS,
        "/storage/v1/b/acme-data/o/a.txt/acl",
        "",
        {"entity": "allUsers"},
        "raw:storage.objects.setIamPolicy",
        "object:acme-data/a.txt",
        "write",
        None,
    ),
    (
        "POST",
        GCS,
        "/upload/storage/v1/b/acme-data/o",
        "uploadType=media&name=out%2Fa.txt&ifGenerationMatch=0",
        b"data",
        "storage.write",
        "object:acme-data/out/a.txt",
        "write",
        None,
    ),
    # BigQuery.
    (
        "GET",
        BQ,
        TBL + "/data",
        "maxResults=10",
        None,
        "bigquery.read",
        "table:my-proj/sales/orders",
        "read",
        "//bigquery.googleapis.com/projects/my-proj/datasets/sales/tables/orders",
    ),
    ("POST", BQ, TBL + "/insertAll", "", {"rows": [{"json": {"a": 1}}]}, "bigquery.write", "table:my-proj/sales/orders", "write", None),
    ("DELETE", BQ, TBL, "", None, "bigquery.delete", "table:my-proj/sales/orders", "destructive", None),
    ("PATCH", BQ, TBL, "", {"description": "x", "etag": "e"}, "raw:bigquery.tables.update", "table:my-proj/sales/orders", "write", None),
    ("POST", BQ, TBL + ":setIamPolicy", "", {"policy": {}}, "iam.set", "table:my-proj/sales/orders", "write", None),
    ("POST", BQ, TBL + ":getIamPolicy", "", {}, "raw:bigquery.tables.getIamPolicy", "table:my-proj/sales/orders", "read", None),
    (
        "POST",
        BQ,
        "/bigquery/v2/projects/my-proj/datasets/sales/tables",
        "",
        {"tableReference": {"projectId": "my-proj", "datasetId": "sales", "tableId": "t2"}, "schema": {}},
        "raw:bigquery.tables.create",
        "dataset:my-proj/sales",
        "write",
        None,
    ),
    ("GET", BQ, "/bigquery/v2/projects/my-proj/datasets/sales", "", None, "raw:bigquery.datasets.get", "dataset:my-proj/sales", "read", None),
    # Secret Manager.
    (
        "GET",
        SM,
        "/v1/projects/my-proj/secrets/db-password/versions/latest:access",
        "",
        None,
        "secret.read",
        "secret:my-proj/db-password",
        "read",
        "//secretmanager.googleapis.com/projects/my-proj/secrets/db-password",
    ),
    ("GET", SM, "/v1/projects/my-proj/secrets/db-password/versions/3", "", None, "raw:secretmanager.versions.get", "secret:my-proj/db-password", "read", None),
    (
        "POST",
        SM,
        "/v1/projects/my-proj/secrets/db-password:addVersion",
        "",
        {"payload": {}},
        "raw:secretmanager.versions.add",
        "secret:my-proj/db-password",
        "write",
        None,
    ),
    ("POST", SM, "/v1/projects/my-proj/secrets/db-password:setIamPolicy", "", {"policy": {}}, "iam.set", "secret:my-proj/db-password", "write", None),
    (
        "GET",
        SM,
        "/v1/projects/my-proj/secrets/db-password:getIamPolicy",
        "",
        None,
        "raw:secretmanager.secrets.getIamPolicy",
        "secret:my-proj/db-password",
        "read",
        None,
    ),
    (
        "POST",
        SM,
        "/v1/projects/my-proj/secrets/db-password/versions/3:destroy",
        "",
        {},
        "raw:secretmanager.versions.destroy",
        "secret:my-proj/db-password",
        "destructive",
        None,
    ),
    ("DELETE", SM, "/v1/projects/my-proj/secrets/db-password", "", None, "raw:secretmanager.secrets.delete", "secret:my-proj/db-password", "destructive", None),
    ("POST", SM, "/v1/projects/my-proj/secrets", "secretId=new", {"replication": {}}, "raw:secretmanager.secrets.create", "project:my-proj", "write", None),
    # IAM and IAM Credentials.
    (
        "POST",
        IAMC,
        f"/v1/projects/-/serviceAccounts/{SA}:generateAccessToken",
        "",
        {"scope": ["https://www.googleapis.com/auth/cloud-platform"]},
        "raw:iam.serviceAccounts.getAccessToken",
        f"serviceaccount:{SA}",
        "write",
        f"//iam.googleapis.com/projects/my-proj/serviceAccounts/{SA}",
    ),
    (
        "POST",
        IAMC,
        f"/v1/projects/-/serviceAccounts/{SA}:signBlob",
        "",
        {"payload": "eA"},
        "raw:iam.serviceAccounts.signBlob",
        f"serviceaccount:{SA}",
        "write",
        None,
    ),
    ("POST", IAM, f"/v1/projects/my-proj/serviceAccounts/{SA}:setIamPolicy", "", {"policy": {}}, "iam.set", f"serviceaccount:{SA}", "write", None),
    ("POST", IAM, f"/v1/projects/my-proj/serviceAccounts/{SA}:disable", "", {}, "raw:iam.serviceAccounts.disable", f"serviceaccount:{SA}", "write", None),
    ("GET", IAM, f"/v1/projects/-/serviceAccounts/{SA}", "", None, "raw:iam.serviceAccounts.get", f"serviceaccount:{SA}", "read", None),
    ("POST", IAM, f"/v1/projects/-/serviceAccounts/{SA}/keys", "", {}, "raw:iam.serviceAccountKeys.create", f"serviceaccount:{SA}", "write", None),
    ("DELETE", IAM, f"/v1/projects/-/serviceAccounts/{SA}/keys/k1", "", None, "raw:iam.serviceAccountKeys.delete", f"serviceaccount:{SA}", "destructive", None),
    ("POST", IAM, "/v1/projects/my-proj/roles", "", {"roleId": "r"}, "raw:iam.roles.create", "project:my-proj", "write", None),
    ("POST", IAM, "/v1/organizations/123456/roles/r1:undelete", "", {}, "raw:iam.roles.undelete", "organization:123456", "write", None),
    # Compute Engine.
    (
        "POST",
        GCE,
        "/compute/v1/projects/my-proj/zones/us-central1-a/instances/web-1/start",
        "",
        None,
        "compute.start",
        "instance:my-proj/us-central1-a/web-1",
        "write",
        None,
    ),
    (
        "POST",
        GCE,
        "/compute/v1/projects/my-proj/zones/us-central1-a/instances/web-1/stop",
        "",
        None,
        "compute.stop",
        "instance:my-proj/us-central1-a/web-1",
        "write",
        None,
    ),
    (
        "DELETE",
        GCE,
        "/compute/v1/projects/my-proj/zones/us-central1-a/instances/web-1",
        "",
        None,
        "compute.delete",
        "instance:my-proj/us-central1-a/web-1",
        "destructive",
        None,
    ),
    (
        "GET",
        GCE,
        "/compute/v1/projects/my-proj/zones/us-central1-a/instances/web-1/getSerialPortOutput",
        "",
        None,
        "raw:compute.instances.getSerialPortOutput",
        "instance:my-proj/us-central1-a/web-1",
        "read",
        None,
    ),
    (
        "POST",
        GCE,
        "/compute/v1/projects/my-proj/zones/us-central1-a/operations/op-1/wait",
        "",
        None,
        "raw:compute.zoneOperations.get",
        "project:my-proj",
        "read",
        None,
    ),
    # Cloud Run, GKE.
    ("GET", RUN, "/v2/projects/my-proj/locations/us-central1/services/api", "", None, "raw:run.services.get", "service:my-proj/us-central1/api", "read", None),
    (
        "DELETE",
        RUN,
        "/v2/projects/my-proj/locations/us-central1/services/api",
        "",
        None,
        "raw:run.services.delete",
        "service:my-proj/us-central1/api",
        "destructive",
        None,
    ),
    (
        "POST",
        RUN,
        "/v2/projects/my-proj/locations/us-central1/services/api:setIamPolicy",
        "",
        {"policy": {}},
        "raw:run.services.setIamPolicy",
        "service:my-proj/us-central1/api",
        "write",
        None,
    ),
    (
        "GET",
        GKE,
        "/v1/projects/my-proj/locations/us-central1/clusters/prod",
        "",
        None,
        "gke.access",
        "cluster:my-proj/us-central1/prod",
        "read",
        "//container.googleapis.com/projects/my-proj/locations/us-central1/clusters/prod",
    ),
    ("GET", GKE, "/v1/projects/my-proj/zones/us-central1-a/clusters/prod", "", None, "gke.access", "cluster:my-proj/us-central1-a/prod", "read", None),
    # Resource Manager.
    ("GET", CRM, "/v3/projects/my-proj", "", None, "project.view", "project:my-proj", "read", None),
    ("POST", CRM, "/v1/projects/my-proj:getIamPolicy", "", {}, "raw:resourcemanager.projects.getIamPolicy", "project:my-proj", "read", None),
    ("POST", CRM, "/v3/projects/my-proj:setIamPolicy", "", {"policy": {}}, "iam.set", "project:my-proj", "write", None),
    ("POST", CRM, "/v3/folders/123:setIamPolicy", "", {"policy": {}}, "iam.set", "folder:123", "write", "//cloudresourcemanager.googleapis.com/folders/123"),
    ("GET", CRM, "/v3/organizations/456", "", None, "raw:resourcemanager.organizations.get", "organization:456", "read", None),
    ("DELETE", CRM, "/v3/projects/my-proj", "", None, "raw:resourcemanager.projects.delete", "project:my-proj", "destructive", None),
    ("POST", CRM, "/v3/projects", "", {"projectId": "new-proj", "parent": "folders/123"}, "raw:resourcemanager.projects.create", "folder:123", "write", None),
]


@pytest.mark.parametrize(("method", "host", "path", "query", "body", "action", "resource", "effect", "full"), ROUTING)
def test_routing(method, host, path, query, body, action, resource, effect, full):
    ct = "text/plain" if isinstance(body, bytes) else "application/json"
    o = ROUTES.match(req(method, host, path, query, body, ct=ct))
    assert o is not None and o.unresolved is None, o
    assert (o.action, o.resource, o.effect) == (action, resource, effect)
    # The action is one the integration checks; the resource parses into the full resource name it asks about.
    assert find_action(GoogleCloud(), action) is not None
    ref = parse_ref(action, parse_resource(resource))
    if full is not None:
        assert ref.resource == full


def test_setting_an_iam_policy_sends_out():
    for method, host, path in [
        ("PUT", GCS, "/storage/v1/b/acme-data/iam"),
        ("POST", CRM, "/v3/projects/my-proj:setIamPolicy"),
        ("POST", SM, "/v1/projects/my-proj/secrets/s:setIamPolicy"),
        ("POST", GCE, "/compute/v1/projects/my-proj/zones/us-central1-a/instances/web-1/setIamPolicy"),
    ]:
        o = ROUTES.match(req(method, host, path, "", {"policy": {}}))
        assert o is not None and o.sends_out and o.unresolved is None, path


@pytest.mark.parametrize(
    ("method", "host", "path", "query", "body"),
    [
        # A path addressed to another service's host.
        ("GET", SM, "/storage/v1/b/acme-data", "", None),
        ("POST", GCE, f"/v1/projects/-/serviceAccounts/{SA}:generateAccessToken", "", {}),
        # Uploads that may overwrite, or set ACLs, or name two objects.
        ("POST", GCS, "/upload/storage/v1/b/acme-data/o", "uploadType=media&name=a.txt", b"x"),
        ("POST", GCS, "/upload/storage/v1/b/acme-data/o", "uploadType=media&name=a.txt&ifGenerationMatch=7", b"x"),
        ("POST", GCS, "/upload/storage/v1/b/acme-data/o", "uploadType=media&name=a.txt&ifGenerationMatch=0&predefinedAcl=publicRead", b"x"),
        ("POST", GCS, "/upload/storage/v1/b/acme-data/o", "uploadType=resumable&name=a.txt&ifGenerationMatch=0", {"name": "b.txt"}),
        ("POST", GCS, "/upload/storage/v1/b/acme-data/o", "uploadType=media&ifGenerationMatch=0", b"x"),
        ("POST", GCS, "/upload/storage/v1/b/acme-data/o", "uploadType=media&name=a%2F..%2Fb&ifGenerationMatch=0", b"x"),
        ("PUT", GCS, "/upload/storage/v1/b/acme-data/o", "uploadType=resumable", b"x"),
        # Metadata a plain update does not cover.
        ("PATCH", GCS, "/storage/v1/b/acme-data", "", {"ipFilter": {}}),
        ("PATCH", GCS, "/storage/v1/b/acme-data", "", {"acl": [], "labels": {}}),
        ("PATCH", GCS, "/storage/v1/b/acme-data/o/a.txt", "", {"retention": {"mode": "Locked"}}),
        ("PATCH", GCS, "/storage/v1/b/acme-data", "", {"labels": {}, "Labels": {}}),
        # Object names toolpass cannot check, and the XML API.
        ("GET", GCS, "/storage/v1/b/acme-data/o/a%2F..%2Fsecret", "", None),
        ("GET", GCS, "/storage/v1/b/acme-data/o/a/b/c", "", None),
        ("GET", GCS, "/acme-data/reports/q1.csv", "", None),
        ("GET", GCS, "/storage/v1/b/acme-data/o/%FF", "", None),
        # BigQuery: SQL, derived tables, second tables, access lists.
        ("POST", BQ, "/bigquery/v2/projects/my-proj/queries", "", {"query": "SELECT 1"}),
        ("POST", BQ, "/bigquery/v2/projects/my-proj/jobs", "", {"configuration": {}}),
        ("GET", BQ, "/bigquery/v2/projects/my-proj/queries/job_1", "", None),
        ("POST", BQ, "/bigquery/v2/projects/my-proj/datasets/sales/tables", "", {"view": {"query": "SELECT * FROM hr.salaries"}}),
        (
            "POST",
            BQ,
            "/bigquery/v2/projects/my-proj/datasets/sales/tables",
            "",
            {"tableReference": {"projectId": "other", "datasetId": "sales", "tableId": "t"}},
        ),
        ("PATCH", BQ, TBL, "", {"externalDataConfiguration": {}}),
        ("POST", BQ, TBL + "/insertAll", "", {"rows": [], "templateSuffix": "_x"}),
        ("PATCH", BQ, "/bigquery/v2/projects/my-proj/datasets/sales", "", {"access": []}),
        ("DELETE", BQ, "/bigquery/v2/projects/my-proj/datasets/sales", "deleteContents=true", None),
        # Secret Manager, IAM.
        ("POST", SM, "/v1/projects/my-proj/secrets/db:frobnicate", "", {}),
        ("GET", SM, "/v1/projects/my-proj/secrets/db/versions/1:destroy", "", None),
        (
            "POST",
            IAMC,
            f"/v1/projects/-/serviceAccounts/{SA}:generateAccessToken",
            "",
            {"delegates": ["projects/-/serviceAccounts/x@y.iam.gserviceaccount.com"]},
        ),
        ("POST", IAMC, "/v1/projects/-/serviceAccounts/1234567890:generateAccessToken", "", {}),
        ("GET", IAM, f"/v1/projects/other-proj/serviceAccounts/{SA}", "", None),
        ("POST", IAMC, f"/v1/projects/-/serviceAccounts/{SA}:setIamPolicy", "", {}),
        # Compute, Cloud Run: code that runs as a service account.
        ("POST", GCE, "/compute/v1/projects/my-proj/zones/us-central1-a/instances/web-1/setMetadata", "", {"items": []}),
        ("POST", GCE, "/compute/v1/projects/my-proj/zones/us-central1-a/instances", "", {"name": "x"}),
        ("PATCH", RUN, "/v2/projects/my-proj/locations/us-central1/services/api", "", {"template": {}}),
        ("POST", RUN, "/v2/projects/my-proj/locations/us-central1/services", "serviceId=x", {"template": {}}),
        # Resource Manager: moves.
        ("POST", CRM, "/v3/projects/my-proj:move", "", {"destinationParent": "folders/9"}),
        ("PATCH", CRM, "/v3/projects/my-proj", "", {"parent": "folders/9"}),
        ("POST", CRM, "/v3/projects", "", {"projectId": "x"}),
        # Any read toolpass does not know.
        ("GET", "pubsub.googleapis.com", "/v1/projects/my-proj/topics", "", None),
        ("GET", GCE, "/compute/v1/projects/my-proj/global/images", "", None),
    ],
)
def test_requests_toolpass_cannot_check_exactly_are_refused(method, host, path, query, body):
    ct = "text/plain" if isinstance(body, bytes) else "application/json"
    o = ROUTES.match(req(method, host, path, query, body, ct=ct))
    assert o is not None and o.unresolved, o


def test_copies_rewrites_and_unknown_writes_have_no_route():
    for method, path in [
        ("POST", "/storage/v1/b/a/o/x/copyTo/b/b/o/y"),
        ("POST", "/storage/v1/b/a/o/x/rewriteTo/b/b/o/y"),
        ("POST", "/storage/v1/b/a/o/x/compose"),
        ("PUT", "/acme-data/a.txt"),  # the XML API
    ]:
        assert ROUTES.match(req(method, GCS, path, "", {})) is None, path
    assert ROUTES.match(req("POST", "policytroubleshooter.googleapis.com", "/v3/iam:troubleshoot", "", {})) is None
    assert ROUTES.is_unrouted("POST", "/v3/iam:troubleshoot")


def test_a_multipart_upload_names_the_object_in_its_metadata():
    meta = json.dumps({"name": "out/report.csv"})
    body = (
        f"--{BOUNDARY}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{meta}\r\n"
        f"--{BOUNDARY}\r\nContent-Type: text/csv\r\n\r\na,b\r\n--{BOUNDARY}--\r\n"
    )
    o = ROUTES.match(
        req(
            "POST",
            GCS,
            "/upload/storage/v1/b/acme-data/o",
            "uploadType=multipart&ifGenerationMatch=0",
            body.encode(),
            ct=f"multipart/related; boundary={BOUNDARY}",
        )
    )
    assert o is not None and o.unresolved is None, o
    assert (o.action, o.resource) == ("storage.write", "object:acme-data/out/report.csv")


def test_a_method_override_and_admin_access_are_refused():
    o = ROUTES.match(req("POST", GCS, "/storage/v1/b/acme-data/o/watch", "", {}, headers={"x-http-method-override": "DELETE"}))
    assert o is not None and o.unresolved
    o = ROUTES.match(req("GET", CRM, "/v3/projects/my-proj", "useAdminAccess=true"))
    assert o is not None and o.unresolved


def test_test_iam_permissions_checks_nothing():
    o = ROUTES.match(req("POST", CRM, "/v3/projects/my-proj:testIamPermissions", "", {"permissions": ["x.y.z"]}))
    assert o is not None and (o.action, o.unresolved, o.effect) == (None, None, "read")


def test_the_hosts_are_googles():
    eps = ROUTES.endpoints(Settings("gcp", "googlecloud", {"scope": "project:my-proj"}))
    assert [(h, "") for h in HOSTS] == eps[: len(HOSTS)]
    assert ("www.googleapis.com", "/storage/v1") in eps
    # The same service under the shared host is refused, not passed unseen.
    o = ROUTES.match(req("GET", "www.googleapis.com", "/storage/v1/b/acme-data/o/a.txt", "alt=media"), "/storage/v1")
    assert o is not None and o.unresolved

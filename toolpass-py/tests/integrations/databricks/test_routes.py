"""The databricks route table: workspace objects by id and Unity Catalog securables by name.

Databricks publishes no OpenAPI description of the workspace API, so every
route is spec=False and the shapes are held here.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest

from tests.integrations.databricks.test_databricks import Env, bob, check, dana
from toolpass.core.catalog import parse_resource
from toolpass.core.decision import Code
from toolpass.core.integration import Settings, find_action
from toolpass.core.routes import Request
from toolpass.integrations.databricks import INTEGRATION
from toolpass.integrations.databricks.actions import parse_ref
from toolpass.integrations.databricks.routes import ROUTES

HOST = "adb-1234567890123456.7.azuredatabricks.net"
JSON = {"content-type": "application/json"}


def req(method: str, path: str, body: Any = None, query: str = "", headers: dict[str, str] | None = None) -> Request:
    data = body if isinstance(body, bytes) else (json.dumps(body).encode() if body is not None else None)
    return Request("https", HOST, 443, method, path, query, JSON if headers is None else headers, data)


def match(r: Request):
    o = ROUTES.match(r)
    assert o is not None, r.describe()
    return o


def test_the_workspace_url_is_where_the_api_lives():
    assert ROUTES.endpoints(Settings("dbx", "databricks", {"url": f"https://{HOST}"})) == [(HOST, "")]


def test_every_route_names_an_action_the_integration_checks():
    for r in ROUTES.routes:
        assert r.action is None or find_action(INTEGRATION, r.action) is not None, r
    assert all(not r.spec for r in ROUTES.routes), "there is no API description to hold a spec=True route to"


CASES = [
    # Jobs, in every API version.
    ("GET", "/api/2.1/jobs/get", None, "job_id=42", "job.view", "job:42", "read"),
    ("GET", "/api/2.2/jobs/get", None, "job_id=42", "job.view", "job:42", "read"),
    ("GET", "/api/2.1/jobs/runs/list", None, "job_id=42&active_only=true", "job.view", "job:42", "read"),
    ("POST", "/api/2.1/jobs/run-now", {"job_id": 42, "job_parameters": {"day": "mon"}}, "", "job.run", "job:42", "write"),
    ("POST", "/api/2.0/jobs/run-now", {"job_id": "42"}, "", "job.run", "job:42", "write"),
    ("POST", "/api/2.2/jobs/runs/cancel-all", {"job_id": 42}, "", "job.run", "job:42", "write"),
    ("POST", "/api/2.1/jobs/delete", {"job_id": 43}, "", "job.manage", "job:43", "destructive"),
    ("GET", "/api/2.1/jobs/list", None, "limit=25", None, "", "read"),
    # Clusters.
    ("GET", "/api/2.0/clusters/get", None, "cluster_id=0123-456789-abcde1f2", "cluster.attach", "cluster:0123-456789-abcde1f2", "read"),
    ("POST", "/api/2.1/clusters/events", {"cluster_id": "0123-456789-abcde1f2"}, "", "cluster.attach", "cluster:0123-456789-abcde1f2", "read"),
    ("POST", "/api/2.0/clusters/start", {"cluster_id": "0123-456789-abcde1f2"}, "", "cluster.restart", "cluster:0123-456789-abcde1f2", "write"),
    ("POST", "/api/2.1/clusters/restart", {"cluster_id": "0123-456789-abcde1f2"}, "", "cluster.restart", "cluster:0123-456789-abcde1f2", "write"),
    ("POST", "/api/2.0/clusters/delete", {"cluster_id": "0123-456789-abcde1f2"}, "", "cluster.restart", "cluster:0123-456789-abcde1f2", "destructive"),
    ("POST", "/api/2.0/clusters/permanent-delete", {"cluster_id": "0123-456789-abcde1f2"}, "", "cluster.manage", "cluster:0123-456789-abcde1f2",
     "destructive"),
    ("POST", "/api/2.0/clusters/resize", {"cluster_id": "0123-456789-abcde1f2", "num_workers": 4}, "", "cluster.manage",
     "cluster:0123-456789-abcde1f2", "write"),
    ("GET", "/api/2.0/libraries/cluster-status", None, "cluster_id=0123-456789-abcde1f2", "cluster.attach", "cluster:0123-456789-abcde1f2", "read"),
    # SQL warehouses, pipelines, Git folders, serving endpoints.
    ("GET", "/api/2.0/sql/warehouses/abc123def456", None, "", "raw:CAN_VIEW", "warehouse:abc123def456", "read"),
    ("POST", "/api/2.0/sql/warehouses/abc123def456/start", None, "", "warehouse.use", "warehouse:abc123def456", "write"),
    ("POST", "/api/2.0/sql/warehouses/abc123def456/stop", None, "", "warehouse.manage", "warehouse:abc123def456", "write"),
    ("DELETE", "/api/2.0/sql/warehouses/abc123def456", None, "", "warehouse.manage", "warehouse:abc123def456", "destructive"),
    ("POST", "/api/2.0/pipelines/0f1e2d3c-aaaa-bbbb-cccc-1234567890ab/updates", {"full_refresh": False}, "", "pipeline.run",
     "pipeline:0f1e2d3c-aaaa-bbbb-cccc-1234567890ab", "write"),
    ("GET", "/api/2.0/pipelines/0f1e2d3c-aaaa-bbbb-cccc-1234567890ab/events", None, "", "raw:CAN_VIEW",
     "pipeline:0f1e2d3c-aaaa-bbbb-cccc-1234567890ab", "read"),
    ("GET", "/api/2.0/repos/123", None, "", "notebook.read", "repo:123", "read"),
    ("PATCH", "/api/2.0/repos/123", {"branch": "main"}, "", "notebook.edit", "repo:123", "write"),
    ("DELETE", "/api/2.0/repos/123", None, "", "raw:CAN_MANAGE", "repo:123", "destructive"),
    ("POST", "/serving-endpoints/churn-model/invocations", {"inputs": [[1, 2]]}, "", "endpoint.query", "endpoint:churn-model", "write"),
    ("GET", "/api/2.0/serving-endpoints/churn-model", None, "", "raw:CAN_VIEW", "endpoint:churn-model", "read"),
    # Access control lists.
    ("GET", "/api/2.0/permissions/jobs/42", None, "", "raw:CAN_MANAGE", "job:42", "read"),
    ("PATCH", "/api/2.0/permissions/clusters/0123-456789-abcde1f2",
     {"access_control_list": [{"user_name": "bob@example.com", "permission_level": "CAN_RESTART"}]}, "", "raw:CAN_MANAGE",
     "cluster:0123-456789-abcde1f2", "write"),
    ("PUT", "/api/2.0/permissions/notebooks/987654321", {"access_control_list": []}, "", "raw:CAN_MANAGE", "notebook:987654321", "write"),
    # Unity Catalog.
    ("GET", "/api/2.1/unity-catalog/catalogs/main", None, "", "catalog.use", "catalog:main", "read"),
    ("GET", "/api/2.1/unity-catalog/schemas/main.sales", None, "", "raw:USE_SCHEMA", "schema:main.sales", "read"),
    ("GET", "/api/2.1/unity-catalog/tables/Main.Sales.Orders", None, "", "table.read", "table:main.sales.orders", "read"),
    ("GET", "/api/2.1/unity-catalog/tables/main.sales.orders/exists", None, "", "table.read", "table:main.sales.orders", "read"),
    ("PATCH", "/api/2.1/unity-catalog/tables/main.sales.orders", {"owner": "data-eng"}, "", "uc.manage", "table:main.sales.orders", "write"),
    ("DELETE", "/api/2.1/unity-catalog/tables/main.sales.orders", None, "", "uc.manage", "table:main.sales.orders", "destructive"),
    ("PATCH", "/api/2.1/unity-catalog/volumes/main.sales.files", {"new_name": "files2"}, "", "uc.manage", "volume:main.sales.files", "write"),
    ("GET", "/api/2.1/unity-catalog/functions/main.sales.fn", None, "", "function.execute", "function:main.sales.fn", "read"),
    ("GET", "/api/2.1/unity-catalog/models/main.sales.churn/versions/3", None, "", "raw:EXECUTE", "model:main.sales.churn", "read"),
    ("PUT", "/api/2.1/unity-catalog/models/main.sales.churn/aliases/champion", {"version_num": 3}, "", "uc.manage", "model:main.sales.churn", "write"),
    ("DELETE", "/api/2.1/unity-catalog/catalogs/main", None, "force=true", "uc.manage", "catalog:main", "destructive"),
    ("GET", "/api/2.1/unity-catalog/permissions/table/main.sales.orders", None, "", "uc.manage", "table:main.sales.orders", "read"),
    ("GET", "/api/2.1/unity-catalog/effective-permissions/SCHEMA/main.sales", None, "", "uc.manage", "schema:main.sales", "read"),
    ("PATCH", "/api/2.1/unity-catalog/permissions/catalog/main", {"changes": [{"principal": "bob@example.com", "add": ["USE_CATALOG"]}]}, "",
     "uc.manage", "catalog:main", "write"),
    ("POST", "/api/2.1/unity-catalog/schemas", {"name": "new", "catalog_name": "main"}, "", "schema.create", "catalog:main", "write"),
    ("POST", "/api/2.1/unity-catalog/volumes", {"name": "v", "catalog_name": "main", "schema_name": "sales", "volume_type": "MANAGED"}, "",
     "raw:CREATE_VOLUME", "schema:main.sales", "write"),
    ("POST", "/api/2.1/unity-catalog/models", {"name": "m", "catalog_name": "main", "schema_name": "sales"}, "", "raw:CREATE_MODEL",
     "schema:main.sales", "write"),
    ("GET", "/api/2.1/unity-catalog/tables", None, "catalog_name=main&schema_name=sales", None, "", "read"),
    # Files in volumes.
    ("GET", "/api/2.0/fs/files/Volumes/main/sales/files/2024/report.csv", None, "", "volume.read", "volume:main.sales.files", "read"),
    ("HEAD", "/api/2.0/fs/files/Volumes/main/sales/files/report.csv", None, "", "volume.read", "volume:main.sales.files", "read"),
    ("PUT", "/api/2.0/fs/files/Volumes/main/sales/files/out%20put.csv", b"a,b\n", "overwrite=true", "volume.write", "volume:main.sales.files", "write"),
    ("DELETE", "/api/2.0/fs/files/Volumes/main/sales/files/old.csv", None, "", "volume.write", "volume:main.sales.files", "destructive"),
    ("GET", "/api/2.0/fs/directories/Volumes/main/sales/files/2024", None, "", "volume.read", "volume:main.sales.files", "read"),
    ("PUT", "/api/2.0/fs/directories/Volumes/main/sales/files/2025", None, "", "volume.write", "volume:main.sales.files", "write"),
    # The agent's own identity.
    ("GET", "/api/2.0/preview/scim/v2/Me", None, "", None, "", "read"),
]  # fmt: skip


@pytest.mark.parametrize(("method", "path", "body", "query", "action", "resource", "effect"), CASES)
def test_routing(method, path, body, query, action, resource, effect):
    headers = {"content-type": "application/octet-stream"} if isinstance(body, bytes) else None
    o = match(req(method, path, body, query, headers))
    assert o.unresolved is None, o.unresolved
    assert (o.action, o.resource, o.effect) == (action, resource, effect)


@pytest.mark.parametrize(("method", "path", "body", "query", "action", "resource", "effect"), [c for c in CASES if c[4]])
def test_resources_parse_as_the_integration_reads_them(method, path, body, query, action, resource, effect):
    parse_ref(action, parse_resource(resource))


@pytest.mark.parametrize(
    ("method", "path", "body", "query"),
    [
        # The id twice, in another spelling, in the wrong place or in a form toolpass does not read exactly.
        ("POST", "/api/2.1/jobs/run-now", b'{"job_id": 42, "job_id": 7}', ""),
        ("POST", "/api/2.1/jobs/run-now", {"job_id": 42, "jobId": 7}, ""),
        ("POST", "/api/2.1/jobs/run-now", {"job_id": 42}, "job_id=7"),
        ("POST", "/api/2.1/jobs/run-now", {"job_id": 42.0}, ""),
        ("POST", "/api/2.1/jobs/run-now", {"job_id": "042"}, ""),
        ("POST", "/api/2.1/jobs/run-now", {"job_id": True}, ""),
        ("POST", "/api/2.1/jobs/run-now", {"job_id": [42]}, ""),
        ("POST", "/api/2.1/jobs/run-now", {}, ""),
        ("POST", "/api/2.1/jobs/run-now", None, ""),
        ("GET", "/api/2.1/jobs/get", None, "job_id=42&job_id=7"),
        ("GET", "/api/2.1/jobs/get", None, "job_id=42&jobId=7"),
        ("GET", "/api/2.1/jobs/get", {"job_id": 7}, "job_id=42"),
        ("GET", "/api/2.1/jobs/get", None, ""),
        ("POST", "/api/2.0/clusters/start", {"cluster_id": "../x"}, ""),
        ("POST", "/api/2.0/clusters/start", {"cluster_id": 12}, ""),
        # Acting beyond the one job.
        ("POST", "/api/2.1/jobs/runs/cancel-all", {"job_id": 42, "all_queued_runs": True}, ""),
        # A run does not say which job it belongs to; a job's runs need the job.
        ("GET", "/api/2.1/jobs/runs/get", None, "run_id=7"),
        ("GET", "/api/2.1/jobs/runs/get-output", None, "run_id=7"),
        ("GET", "/api/2.1/jobs/runs/list", None, ""),
        # Results of code and SQL that run as the agent.
        ("GET", "/api/2.0/sql/statements/01ef-aaaa", None, ""),
        ("GET", "/api/2.0/sql/statements/01ef-aaaa/result/chunks/1", None, ""),
        ("GET", "/api/1.2/commands/status", None, "clusterId=x&contextId=y&commandId=z"),
        # Paths toolpass cannot check, and secrets.
        ("GET", "/api/2.0/workspace/export", None, "path=/Users/ceo/plan"),
        ("GET", "/api/2.0/dbfs/read", None, "path=/x"),
        ("GET", "/api/2.0/secrets/get", None, "scope=prod&key=db"),
        # Unity Catalog names toolpass does not read.
        ("GET", "/api/2.1/unity-catalog/tables/main.sales", None, ""),
        ("GET", "/api/2.1/unity-catalog/tables/main.sales.orders.x", None, ""),
        ("GET", "/api/2.1/unity-catalog/tables/main.sales.`or ders`", None, ""),
        ("GET", "/api/2.1/unity-catalog/tables/main.sales%2Forders.x", None, ""),
        ("GET", "/api/2.1/unity-catalog/permissions/external_location/loc", None, ""),
        ("PATCH", "/api/2.1/unity-catalog/schemas/main.sales", {"new_name": "hr.sales"}, ""),
        ("PATCH", "/api/2.1/unity-catalog/schemas/main.sales", {"new_name": "x", "newName": "y"}, ""),
        # New securables reaching outside storage, or a parent given twice.
        ("POST", "/api/2.1/unity-catalog/schemas", {"name": "n", "catalog_name": "main", "storage_root": "s3://bucket/x"}, ""),
        ("POST", "/api/2.1/unity-catalog/volumes", {"name": "v", "catalog_name": "main", "schema_name": "sales", "storage_location": "s3://b"}, ""),
        ("POST", "/api/2.1/unity-catalog/volumes", {"name": "v", "catalog_name": "main", "schema_name": "sales", "schemaName": "hr"}, ""),
        ("POST", "/api/2.1/unity-catalog/schemas", {"name": "n"}, ""),
        # Permissions on objects toolpass does not check, and owner changes.
        ("GET", "/api/2.0/permissions/authorization/tokens", None, ""),
        ("PATCH", "/api/2.0/permissions/jobs/42", {"access_control_list": [{"user_name": "bob@example.com", "permission_level": "IS_OWNER"}]}, ""),
        # Files outside one volume, or climbing out of it.
        ("GET", "/api/2.0/fs/files/Volumes/main/sales/files/../../hr/salaries/x.csv", None, ""),
        ("GET", "/api/2.0/fs/files/Volumes/main/sales/files%2F..%2F..%2Fhr/x.csv", None, ""),
        ("PUT", "/api/2.0/fs/files/Volumes/main/sales/files//x.csv", b"x", ""),
        ("GET", "/api/2.0/fs/files/Volumes/main/sales/files//x.csv", None, ""),
        ("GET", "/api/2.0/fs/directories/Volumes//sales/files", None, ""),
        ("GET", "/api/2.0/fs/files/Volumes/main/sales", None, ""),
        ("GET", "/api/2.0/fs/files/volumes/main/sales/files/x.csv", None, ""),
        ("GET", "/api/2.0/fs/files/tmp/x.csv", None, ""),
        # A body toolpass does not read as JSON.
        ("POST", "/api/2.1/jobs/delete", b"job_id=42", ""),
    ],
)
def test_requests_toolpass_cannot_check_exactly_are_refused(method, path, body, query):
    headers = {"content-type": "application/x-www-form-urlencoded"} if body == b"job_id=42" else None
    o = match(req(method, path, body, query, headers))
    assert o.unresolved, o


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/api/2.0/sql/statements"),
        ("POST", "/api/1.2/commands/execute"),
        ("POST", "/api/2.1/jobs/runs/submit"),
        ("POST", "/api/2.1/jobs/runs/cancel"),
        ("POST", "/api/2.1/jobs/runs/repair"),
        ("POST", "/api/2.1/jobs/update"),
        ("POST", "/api/2.1/jobs/reset"),
        ("POST", "/api/2.1/jobs/create"),
        ("POST", "/api/2.0/clusters/edit"),
        ("POST", "/api/2.0/clusters/create"),
        ("POST", "/api/2.0/clusters/change-owner"),
        ("PUT", "/api/2.0/pipelines/0f1e2d3c-aaaa-bbbb-cccc-1234567890ab"),
        ("POST", "/api/2.0/workspace/import"),
        ("POST", "/api/2.0/workspace/delete"),
        ("POST", "/api/2.0/dbfs/put"),
        ("POST", "/api/2.0/secrets/put"),
        ("POST", "/api/2.0/libraries/install"),
        ("POST", "/api/2.0/token/create"),
        ("POST", "/api/2.1/unity-catalog/catalogs"),
        ("POST", "/api/2.1/unity-catalog/tables"),
        ("POST", "/api/2.1/unity-catalog/functions"),
        ("POST", "/api/2.1/unity-catalog/external-locations"),
    ],
)
def test_writes_left_out_on_purpose_have_no_route(method, path):
    assert ROUTES.match(req(method, path, {})) is None, path
    assert ROUTES.is_unrouted(method, path), path


def test_lists_are_reads_without_a_check_and_untrusted():
    for path in ("/api/2.1/jobs/list", "/api/2.0/clusters/list", "/api/2.1/unity-catalog/catalogs", "/api/2.0/workspace/list"):
        o = match(req("GET", path))
        assert (o.action, o.effect, o.untrusted_output, o.unresolved) == (None, "read", True, None), path


def test_invocations_send_data_out():
    assert match(req("POST", "/serving-endpoints/churn-model/invocations", {"inputs": []})).sends_out


@pytest.fixture
def dbx() -> Iterator[Env]:
    e = Env()
    yield e
    e.close()


def test_routed_requests_are_checked_on_the_users_permissions(dbx: Env):
    _, _, c = dbx.setup()

    def decide(user: Any, method: str, path: str, body: Any = None, query: str = "") -> Code:
        o = match(req(method, path, body, query))
        assert o.unresolved is None and o.action and o.resource, o
        return check(c, user, o.action, o.resource).code

    assert decide(dana, "POST", "/api/2.1/jobs/run-now", {"job_id": 42}) == Code.ALLOWED
    assert decide(bob, "POST", "/api/2.1/jobs/run-now", {"job_id": 42}) == Code.DENIED
    assert decide(bob, "GET", "/api/2.1/jobs/get", query="job_id=42") == Code.ALLOWED
    assert decide(dana, "POST", "/api/2.0/clusters/restart", {"cluster_id": "0123-456789-abcde1f2"}) == Code.ALLOWED
    assert decide(bob, "POST", "/api/2.0/clusters/restart", {"cluster_id": "0123-456789-abcde1f2"}) == Code.DENIED
    assert decide(dana, "GET", "/api/2.1/unity-catalog/tables/main.sales.orders") == Code.ALLOWED
    assert decide(bob, "GET", "/api/2.1/unity-catalog/tables/main.sales.orders") == Code.DENIED
    assert decide(dana, "PUT", "/api/2.0/fs/files/Volumes/main/sales/files/x.csv", {}) == Code.ALLOWED
    assert decide(bob, "GET", "/api/2.0/fs/files/Volumes/main/sales/files/x.csv") == Code.DENIED
    assert decide(dana, "POST", "/api/2.1/unity-catalog/schemas", {"name": "n", "catalog_name": "main"}) == Code.ALLOWED

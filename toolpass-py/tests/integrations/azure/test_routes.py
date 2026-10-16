"""The azure route table: ARM paths mapped to RBAC operations on scopes, and
held to the Microsoft.Authorization API descriptions."""

from __future__ import annotations

import json

import pytest

from tests.harness.routes import check_routes_against_spec
from toolpass.core.catalog import parse_resource
from toolpass.core.integration import Settings, find_action
from toolpass.core.routes import Request
from toolpass.integrations.azure import INTEGRATION
from toolpass.integrations.azure.actions import parse_target
from toolpass.integrations.azure.routes import ROUTES

SUB = "00000000-0000-0000-0000-0000000000aa"
S = f"/subscriptions/{SUB}"
RG = f"{S}/resourceGroups/shop"
VM = f"{RG}/providers/Microsoft.Compute/virtualMachines/web-1"
RA = "providers/Microsoft.Authorization/roleAssignments/11111111-2222-3333-4444-555555555555"
READER = f"{S}/providers/Microsoft.Authorization/roleDefinitions/acdd72a7-3385-48ef-bd42-f606fba81ae7"


def req(method: str, path: str, body: object = None, headers: dict[str, str] | None = None) -> Request:
    raw = json.dumps(body).encode() if body is not None and not isinstance(body, bytes) else body
    return Request("https", "management.azure.com", 443, method, path, "api-version=2022-04-01", {"content-type": "application/json", **(headers or {})}, raw)


def match(r: Request):
    o = ROUTES.match(r)
    assert o is not None, r.describe()
    return o


def test_the_integration_exposes_the_table_on_its_arm_endpoint():
    assert INTEGRATION.routes() is ROUTES
    assert ROUTES.endpoints(Settings("az", "azure", {"url": "https://management.azure.com"})) == [("management.azure.com", "")]
    assert ROUTES.endpoints(Settings("az", "azure", {"url": "https://management.usgovcloudapi.net/"})) == [("management.usgovcloudapi.net", "")]


@pytest.mark.parametrize("name", ["azure-RoleAssignmentsCalls", "azure-RoleDefinitionsCalls", "azure-DenyAssignmentCalls"])
def test_routes_match_the_api_description(name):
    # Every write in Microsoft.Authorization's description is routed.
    assert check_routes_against_spec(ROUTES, name, host="management.azure.com") == []


@pytest.mark.parametrize(
    ("method", "path", "body", "action", "resource", "effect"),
    [
        ("GET", S, None, "Microsoft.Resources/subscriptions/read", f"subscription:{SUB}", "read"),
        ("GET", f"{S}/resourceGroups", None, "Microsoft.Resources/subscriptions/resourceGroups/read", f"subscription:{SUB}", "read"),
        ("PUT", RG, {"location": "westeurope"}, "Microsoft.Resources/subscriptions/resourceGroups/write", f"resourcegroup:{SUB}/shop", "write"),
        ("HEAD", RG, None, "Microsoft.Resources/subscriptions/resourceGroups/read", f"resourcegroup:{SUB}/shop", "read"),
        ("DELETE", RG, None, "Microsoft.Resources/subscriptions/resourceGroups/delete", f"resourcegroup:{SUB}/shop", "destructive"),
        (
            "DELETE",
            RG.replace("resourceGroups", "resourcegroups").upper().replace(SUB.upper(), SUB),
            None,
            "Microsoft.Resources/subscriptions/resourceGroups/delete",
            f"resourcegroup:{SUB}/SHOP",
            "destructive",
        ),
        ("POST", f"{RG}/exportTemplate", None, "Microsoft.Resources/subscriptions/resourceGroups/exportTemplate/action", f"resourcegroup:{SUB}/shop", "write"),
        ("GET", f"{RG}/resources", None, "Microsoft.Resources/subscriptions/resourceGroups/resources/read", f"resourcegroup:{SUB}/shop", "read"),
        ("GET", f"{RG}/providers/Microsoft.Compute/virtualMachines", None, "Microsoft.Compute/virtualMachines/read", f"resourcegroup:{SUB}/shop", "read"),
        ("GET", VM, None, "Microsoft.Compute/virtualMachines/read", f"resource:{VM}", "read"),
        ("PATCH", VM, {"tags": {"env": "prod"}}, "Microsoft.Compute/virtualMachines/write", f"resource:{VM}", "write"),
        ("DELETE", VM, None, "Microsoft.Compute/virtualMachines/delete", f"resource:{VM}", "destructive"),
        ("POST", f"{VM}/start", None, "Microsoft.Compute/virtualMachines/start/action", f"resource:{VM}", "write"),
        ("POST", f"{VM}/deallocate", None, "Microsoft.Compute/virtualMachines/deallocate/action", f"resource:{VM}", "write"),
        ("GET", f"{VM}/instanceView", None, "Microsoft.Compute/virtualMachines/instanceView/read", f"resource:{VM}", "read"),
        (
            "POST",
            f"{RG}/providers/Microsoft.Storage/storageAccounts/shopdata/listKeys",
            None,
            "Microsoft.Storage/storageAccounts/listKeys/action",
            f"resource:{RG}/providers/Microsoft.Storage/storageAccounts/shopdata",
            "write",
        ),
        (
            "POST",
            f"{RG}/providers/Microsoft.ContainerService/managedClusters/aks/listClusterAdminCredential",
            None,
            "Microsoft.ContainerService/managedClusters/listClusterAdminCredential/action",
            f"resource:{RG}/providers/Microsoft.ContainerService/managedClusters/aks",
            "write",
        ),
        (
            "PUT",
            f"{RG}/providers/Microsoft.Sql/servers/db1/databases/orders",
            {"location": "westeurope"},
            "Microsoft.Sql/servers/databases/write",
            f"resource:{RG}/providers/Microsoft.Sql/servers/db1/databases/orders",
            "write",
        ),
        (
            "GET",
            f"{RG}/providers/Microsoft.Sql/servers/db1/databases",
            None,
            "Microsoft.Sql/servers/databases/read",
            f"resource:{RG}/providers/Microsoft.Sql/servers/db1",
            "read",
        ),
        (
            "POST",
            f"{RG}/providers/Microsoft.KeyVault/locations/westeurope/deletedVaults/old/purge",
            None,
            "Microsoft.KeyVault/locations/deletedVaults/purge/action",
            f"resource:{RG}/providers/Microsoft.KeyVault/locations/westeurope/deletedVaults/old",
            "destructive",
        ),
        ("POST", f"{S}/providers/Microsoft.Compute/register", None, "Microsoft.Compute/register/action", f"subscription:{SUB}", "write"),
        ("GET", f"{S}/providers", None, "Microsoft.Resources/subscriptions/providers/read", f"subscription:{SUB}", "read"),
        (
            "GET",
            f"{S}/providers/Microsoft.Compute/locations/westeurope/operations/op1",
            None,
            "Microsoft.Compute/locations/operations/read",
            f"subscription:{SUB}",
            "read",
        ),
        # Role assignments: at a resource exactly, at a subscription or management group on that scope.
        (
            "PUT",
            f"{VM}/{RA}",
            {"properties": {"roleDefinitionId": READER, "principalId": "99999999-9999-9999-9999-999999999999"}},
            "Microsoft.Authorization/roleAssignments/write",
            f"resource:{VM}/{RA}",
            "write",
        ),
        (
            "PUT",
            f"{RG}/{RA}",
            {"properties": {"roleDefinitionId": READER, "principalId": "9" * 8}},
            "Microsoft.Authorization/roleAssignments/write",
            f"resource:{RG}/{RA}",
            "write",
        ),
        (
            "PUT",
            f"{S}/{RA}",
            {"properties": {"roleDefinitionId": READER, "principalId": "9" * 8}},
            "Microsoft.Authorization/roleAssignments/write",
            f"subscription:{SUB}",
            "write",
        ),
        ("DELETE", f"{S}/{RA}", None, "Microsoft.Authorization/roleAssignments/delete", f"subscription:{SUB}", "destructive"),
        ("GET", f"{VM}/providers/Microsoft.Authorization/roleAssignments", None, "Microsoft.Authorization/roleAssignments/read", f"resource:{VM}", "read"),
        (
            "PUT",
            f"/providers/Microsoft.Management/managementGroups/root-mg/{RA}",
            {"properties": {"roleDefinitionId": "/providers/Microsoft.Authorization/roleDefinitions/acdd72a7-3385-48ef-bd42-f606fba81ae7"}},
            "Microsoft.Authorization/roleAssignments/write",
            "managementgroup:root-mg",
            "write",
        ),
        (
            "PUT",
            f"{S}/providers/Microsoft.Authorization/roleDefinitions/acdd72a7-3385-48ef-bd42-f606fba81ae7",
            {"properties": {"roleName": "ops", "assignableScopes": [S], "permissions": [{"actions": ["Microsoft.Compute/*/read"]}]}},
            "Microsoft.Authorization/roleDefinitions/write",
            f"subscription:{SUB}",
            "write",
        ),
        (
            "PUT",
            f"{RG}/providers/Microsoft.Authorization/locks/no-delete",
            {"properties": {"level": "CanNotDelete"}},
            "Microsoft.Authorization/locks/write",
            f"resource:{RG}/providers/Microsoft.Authorization/locks/no-delete",
            "write",
        ),
        (
            "DELETE",
            f"{RG}/providers/Microsoft.Resources/deployments/old",
            None,
            "Microsoft.Resources/deployments/delete",
            f"resource:{RG}/providers/Microsoft.Resources/deployments/old",
            "destructive",
        ),
        # The body may name the target itself and scopes above it.
        ("PUT", VM, {"id": VM, "properties": {"note": RG}}, "Microsoft.Compute/virtualMachines/write", f"resource:{VM}", "write"),
    ],
)
def test_routing(method, path, body, action, resource, effect):
    o = match(req(method, path, body))
    assert o.unresolved is None, o.unresolved
    assert (o.action, o.resource, o.effect) == ("raw:" + action, resource, effect)
    # The action is one the azure integration checks, on a scope its own parser takes.
    assert find_action(INTEGRATION, o.action) is not None
    parse_target(o.action, parse_resource(o.resource))


@pytest.mark.parametrize(
    "path",
    [
        "/subscriptions",
        "/tenants",
        "/providers",
        "/providers/Microsoft.Compute",
        "/providers/Microsoft.Compute/operations",
        "/providers/Microsoft.Management/managementGroups",
    ],
)
def test_tenant_level_reads_check_nothing(path):
    o = match(req("GET", path))
    assert (o.action, o.unresolved, o.effect) == (None, None, "read")


def test_actions_send_out_and_their_output_is_untrusted():
    o = match(req("POST", f"{VM}/runCommand", {"commandId": "RunShellScript"}))
    assert o.sends_out and o.untrusted_output
    o = match(req("PUT", RG, {"location": "westeurope"}))
    assert not o.sends_out and not o.untrusted_output
    assert match(req("GET", VM)).untrusted_output


@pytest.mark.parametrize(
    ("method", "path", "body", "why"),
    [
        # Paths that do not parse exactly.
        ("GET", f"{VM}%2Fextensions", None, "percent-encoded"),
        ("DELETE", f"{RG}/providers/Microsoft.Compute/virtualMachines/web 1", None, "not an ARM name"),
        ("DELETE", f"{RG}/providers/Microsoft.Compute/virtualMachines/..", None, "not an ARM name"),
        ("DELETE", f"{RG}//providers/Microsoft.Compute/virtualMachines/web-1", None, "not an ARM name"),
        ("DELETE", f"{RG}/providers/Microsoft.Compute/virtualMachines/web-1/", None, "not an ARM name"),
        ("DELETE", "/subscriptions/not-a-guid/resourceGroups/shop", None, "GUID"),
        ("GET", f"{RG}/providers", None, "provider segments"),
        ("DELETE", f"{VM}/providers", None, "provider segments"),
        ("DELETE", f"{RG}/providers/Microsoft.Compute", None, "names a resource provider"),
        ("DELETE", f"{RG}/providers/Microsoft.Compute/virtualMachines", None, "must name"),
        ("POST", VM, None, "name an action"),
        ("PUT", f"{RG}/providers/Microsoft.Compute/virtualMachines(x)/web-1", {}, "integration can check"),
        # Endpoints and scopes the integration cannot check.
        ("POST", "/batch", {"requests": [{"httpMethod": "DELETE", "url": RG}]}, "batch"),
        ("POST", "/providers/Microsoft.Authorization/elevateAccess", None, "tenant root"),
        ("POST", "/providers/Microsoft.ResourceGraph/resources", {"query": "Resources"}, "tenant root"),
        ("GET", "/providers/Microsoft.Capacity/reservationOrders/0000", None, "tenant root"),
        ("GET", "/providers/Microsoft.Authorization/roleAssignments", None, "tenant root"),
        ("PUT", "/providers/Microsoft.Management/managementGroups/root-mg/subscriptions/" + SUB, None, "old parent"),
        # Writes that reach more than the path names.
        ("POST", f"{RG}/moveResources", {"resources": [VM], "targetResourceGroup": f"{S}/resourceGroups/other"}, "move"),
        ("PUT", f"{RG}/providers/Microsoft.Resources/deployments/rollout", {"properties": {"mode": "Incremental", "template": {}}}, "template"),
        ("PUT", f"{S}/providers/Microsoft.Resources/deployments/rollout", {"properties": {"template": {}}}, "template"),
        ("PUT", f"{RG}/providers/Microsoft.Resources/deploymentStacks/s", {"properties": {}}, "template"),
        (
            "PUT",
            f"{RG}/providers/Microsoft.Network/networkInterfaces/nic-1",
            {
                "properties": {
                    "ipConfigurations": [{"properties": {"subnet": {"id": f"{S}/resourceGroups/net/providers/Microsoft.Network/virtualNetworks/v/subnets/s"}}}]
                }
            },
            "another Azure resource",
        ),
        (
            "PATCH",
            VM,
            {"identity": {"type": "UserAssigned", "userAssignedIdentities": {f"{RG}/providers/Microsoft.ManagedIdentity/userAssignedIdentities/admin": {}}}},
            "another Azure resource",
        ),
        (
            "PUT",
            f"{S}/{RA}",
            {
                "properties": {
                    "roleDefinitionId": READER,
                    "principalId": "9" * 8,
                    "delegatedManagedIdentityResourceId": f"{RG}/providers/Microsoft.ManagedIdentity/userAssignedIdentities/x",
                }
            },
            "another Azure resource",
        ),
        ("PUT", f"{S}/{RA}", {"properties": {"description": READER}}, "another Azure resource"),
        (
            "PUT",
            f"{S}/providers/Microsoft.Authorization/roleDefinitions/acdd72a7-3385-48ef-bd42-f606fba81ae7",
            {"properties": {"assignableScopes": ["/subscriptions/00000000-0000-0000-0000-0000000000bb"]}},
            "another Azure resource",
        ),
        (
            "PUT",
            "/providers/Microsoft.Management/managementGroups/child",
            {"properties": {"details": {"parent": {"id": "/providers/Microsoft.Management/managementGroups/other"}}}},
            "another Azure resource",
        ),
        # A duplicate member is seen whichever copy the server keeps.
        (
            "PUT",
            VM,
            b'{"properties": {"availabilitySet": {"id": "/subscriptions/x/resourceGroups/y/providers/Microsoft.Compute/availabilitySets/a"},'
            b' "availabilitySet": {}}}',
            "another Azure resource",
        ),
        ("PUT", VM, b"{'location': 'westeurope'}", "not JSON"),
        ("PUT", VM, b"\xef\xbb\xbf{}", "not JSON"),
    ],
)
def test_refusals(method, path, body, why):
    o = match(req(method, path, body))
    assert o.unresolved is not None and why in o.unresolved, o.unresolved


def test_a_write_body_must_be_json_and_a_method_override_is_refused():
    o = match(Request("https", "management.azure.com", 443, "PUT", VM, "api-version=1", {"content-type": "text/plain"}, b"{}"))
    assert o.unresolved and "application/json" in o.unresolved
    o = match(req("POST", f"{VM}/start", None, {"x-http-method-override": "DELETE"}))
    assert o.unresolved and "override" in o.unresolved


def test_other_methods_are_not_routed():
    assert ROUTES.match(req("OPTIONS", VM)) is None
    assert ROUTES.match(req("GET", "/")) is None

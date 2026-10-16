"""AWS API requests, mapped to the IAM action and ARN that
iam:SimulatePrincipalPolicy is asked about.

The service comes from the host (``<service>.<region>.amazonaws.com``, the
global IAM and STS hosts), the operation from the protocol: the method,
path and query for S3 (path-style) and Lambda, ``Action=`` in the query or
form body for IAM and STS, ``X-Amz-Target`` for Secrets Manager. The check
is ``raw:<service>:<Action>`` on the ARN the operation acts on, read from
the request exactly, or on ``all`` for an action IAM authorizes with no
resource (``iam:ListRoles``, ``s3:ListAllMyBuckets``). Covered:

- S3: bucket and object requests on ``arn:aws:s3:::<bucket>[/<key>]``, with
  the IAM action S3 authorizes (``ListObjectsV2`` is ``s3:ListBucket``, a
  read with ``versionId`` is ``s3:GetObjectVersion``). Only path-style
  requests reach a host the table claims (the regional, dualstack,
  dash-style and Transfer Acceleration hosts); configure the client with
  path-style addressing (virtual-hosted buckets are hosts of their own,
  which exact endpoints cannot claim). An ``x-amz-*`` query parameter other
  than the SigV4 ones of a presigned URL is read as that header, as S3 does.
- Lambda: function requests on the function's ARN (with its qualifier),
  plus layers, code signing configs and tags by ARN.
- Secrets Manager: secret requests on the secret's full ARN (Secrets
  Manager takes a SecretId ending in a hyphen and six characters as a full
  ARN). A secret whose name itself ends that way is refused: its full ARN
  could also be read as a partial ARN of another secret.
- STS: ``AssumeRole`` on the role's ARN (``GetSessionToken``, which IAM
  policies do not authorize, is refused); IAM: managed policies, OIDC and
  SAML providers and virtual MFA devices by ARN, and the account-wide
  actions.

The table cannot see the connection's account id, so a resource named only
by name is refused with a message asking for its ARN: a Lambda function, a
secret, and every IAM user, role and group (whose ARNs also carry an IAM
path the request does not). EC2, RDS, EKS and SSM name their resources that
way throughout, so their hosts are not claimed.

Also refused: a request that reaches a second resource or needs a second
action (an S3 copy, a bulk delete, an upload that sets an ACL, tags or an
object lock, a KMS key named in the request, a Lambda configuration that
passes a role or adds layers, ``sts:AssumeRole`` with session tags),
ambiguous parameters (a name given twice, an S3 request naming two
subresources), unknown S3 subresources, and any operation not in the
table. S3 writes and Lambda invocations are treated as sending data out: a
bucket or a function in another account takes what its policy allows.
"""

from __future__ import annotations

import json
import re
import urllib.parse
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, NamedTuple

from toolpass.core.catalog import parse_resource as parse_resource_ref
from toolpass.core.routes import PathTemplate, Request, Route, RouteTable, matching_templates
from toolpass.integrations.aws.actions import parse_raw, parse_resource

__all__ = [
    "IAM",
    "IAM_UNROUTED",
    "LAMBDA",
    "LAMBDA_PATHS",
    "LAMBDA_UNROUTED",
    "ROUTES",
    "S3",
    "S3_UNROUTED",
    "SECRETSMANAGER",
    "SECRETSMANAGER_UNROUTED",
    "STS",
    "STS_UNROUTED",
    "place",
]

# -- where a request goes ------------------------------------------------------

_REGIONS = {
    "aws": [
        "af-south-1",
        "ap-east-1",
        "ap-east-2",
        "ap-northeast-1",
        "ap-northeast-2",
        "ap-northeast-3",
        "ap-south-1",
        "ap-south-2",
        "ap-southeast-1",
        "ap-southeast-2",
        "ap-southeast-3",
        "ap-southeast-4",
        "ap-southeast-5",
        "ap-southeast-6",
        "ap-southeast-7",
        "ca-central-1",
        "ca-west-1",
        "eu-central-1",
        "eu-central-2",
        "eu-north-1",
        "eu-south-1",
        "eu-south-2",
        "eu-west-1",
        "eu-west-2",
        "eu-west-3",
        "il-central-1",
        "me-central-1",
        "me-south-1",
        "mx-central-1",
        "sa-east-1",
        "us-east-1",
        "us-east-2",
        "us-west-1",
        "us-west-2",
    ],
    "aws-cn": ["cn-north-1", "cn-northwest-1"],
    "aws-us-gov": ["us-gov-east-1", "us-gov-west-1"],
}
_REGION_RE = re.compile(r"[a-z]{2}(-gov)?-[a-z]+-[0-9]")
_SERVICES = ("s3", "sts", "lambda", "secretsmanager")
_GLOBAL = {
    "iam.amazonaws.com": ("iam", "us-east-1"),
    "iam-fips.amazonaws.com": ("iam", "us-east-1"),
    "iam.us-gov.amazonaws.com": ("iam", "us-gov-west-1"),
    "iam.cn-north-1.amazonaws.com.cn": ("iam", "cn-north-1"),
    "sts.amazonaws.com": ("sts", "us-east-1"),
    "s3.amazonaws.com": ("s3", "us-east-1"),
    "s3-external-1.amazonaws.com": ("s3", "us-east-1"),
    # Transfer Acceleration: the bucket's ARN does not name a region.
    "s3-accelerate.amazonaws.com": ("s3", "us-east-1"),
    "s3-accelerate.dualstack.amazonaws.com": ("s3", "us-east-1"),
}
_HOST_RE = re.compile(
    r"(?P<svc>s3|sts|lambda|secretsmanager)(-fips)?(\.dualstack)?\.(?P<region>[a-z]{2}(-gov)?-[a-z]+-[0-9])\.(?P<domain>amazonaws\.com|amazonaws\.com\.cn|api\.aws)"
)
# The legacy dash-style regional S3 hosts (s3-us-west-2.amazonaws.com).
_S3_DASH_RE = re.compile(r"s3-(?P<region>[a-z]{2}(-gov)?-[a-z]+-[0-9])\.amazonaws\.com")


def _partition(region: str) -> str:
    if region.startswith("cn-"):
        return "aws-cn"
    if region.startswith("us-gov-"):
        return "aws-us-gov"
    return "aws"


class _Place(NamedTuple):
    service: str
    region: str
    partition: str


def place(host: str) -> _Place | None:
    """The service, region and partition a host serves, or None."""
    host = host.lower()
    g = _GLOBAL.get(host)
    if g is not None:
        return _Place(g[0], g[1], _partition(g[1]))
    m = _S3_DASH_RE.fullmatch(host)
    if m is not None:
        region = m.group("region")
        return None if region.startswith("cn-") else _Place("s3", region, _partition(region))
    m = _HOST_RE.fullmatch(host)
    if m is None:
        return None
    region = m.group("region")
    if (m.group("domain") == "amazonaws.com.cn") != region.startswith("cn-"):
        return None
    return _Place(m.group("svc"), region, _partition(region))


def _locate(settings: Any) -> Sequence[tuple[str, str]]:
    """Every regional host of the covered services in the connection's
    partition (with S3's dualstack and dash-style ones), and the global IAM,
    STS and S3 hosts (with Transfer Acceleration's).

    Endpoints are exact hosts, so a virtual-hosted bucket
    (``<bucket>.s3[.<region>].amazonaws.com``, ``<bucket>.s3-accelerate...``)
    cannot be claimed: every bucket name is a host of its own. Such requests
    reach no route; only ``strict`` refuses them."""
    get = getattr(settings, "get", None)
    partition = (get("partition") if callable(get) else "") or "aws"
    region = get("region") if callable(get) else ""
    regions = list(_REGIONS.get(partition, ()))
    if region and _REGION_RE.fullmatch(region) and _partition(region) == partition and region not in regions:
        regions.append(region)
    suffix = "amazonaws.com.cn" if partition == "aws-cn" else "amazonaws.com"
    hosts = [h for h, (_, r) in _GLOBAL.items() if _partition(r) == partition]
    for r in regions:
        for svc in _SERVICES:
            hosts += [f"{svc}.{r}.{suffix}", f"{svc}-fips.{r}.{suffix}"]
        hosts += [f"s3.dualstack.{r}.{suffix}", f"s3-fips.dualstack.{r}.{suffix}"]
        if partition != "aws-cn":
            hosts.append(f"s3-{r}.{suffix}")
        if partition != "aws-cn":
            hosts.append(f"lambda.{r}.api.aws")
    return [(h, "") for h in hosts]


# -- results -------------------------------------------------------------------


def _unresolved(reason: str, operation: str = "") -> dict[str, str]:
    out = {"_unresolved": reason}
    if operation:
        out["operation"] = operation
    return out


def _ok(service: str, action: str, resource: str, effect: str, operation: str, partition: str) -> dict[str, str]:
    """The observed action, once the integration's own parsers accept it."""
    name = f"raw:{service}:{action}"
    try:
        parse_raw(name)
        if resource != "all":
            parse_resource(parse_resource_ref(resource), partition)
    except Exception:  # noqa: BLE001 - whatever the integration refuses, the table does not send
        return _unresolved(f"{resource} is not an ARN the aws integration can check", operation)
    return {"_action": name, "_resource": resource, "_effect": effect, "operation": operation}


def _unchecked(operation: str) -> dict[str, str]:
    return {"_action": "", "_resource": "", "_effect": "read", "operation": operation}


_ACCOUNT_RE = re.compile(r"[0-9]{12}")
_ARN_TEXT_RE = re.compile(r"[\x21-\x3e\x40-\x7e]+")  # printable ASCII, no "?" (a resource query)


def _arn(value: str, p: _Place, service: str, kinds: Sequence[str], regional: bool) -> str | None:
    """value when it is an ARN of ``service`` in the request's partition (and
    region, for a regional service), in a 12-digit account, whose resource
    part starts with one of ``kinds``."""
    parts = value.split(":", 5)
    if len(parts) != 6 or parts[0] != "arn" or parts[1] != p.partition or parts[2] != service:
        return None
    if parts[3] != (p.region if regional else "") or not _ACCOUNT_RE.fullmatch(parts[4]):
        return None
    if not _ARN_TEXT_RE.fullmatch(parts[5]) or not any(parts[5].startswith(k) and len(parts[5]) > len(k) for k in kinds):
        return None
    return value


class _Obj(list[tuple[str, Any]]):
    """A JSON object as its members in order, duplicates kept."""


def _json_body(body: bytes | None) -> Any:
    """The body as JSON with every object as an _Obj; ValueError when it is not JSON."""
    if not body:
        return _Obj()
    return json.loads(body.decode("utf-8"), object_pairs_hook=_Obj)


def _names(doc: Any) -> set[str]:
    """Every member name anywhere in a JSON document, case-folded."""
    out: set[str] = set()
    stack = [doc]
    while stack:
        v = stack.pop()
        if isinstance(v, _Obj):
            for k, item in v:
                out.add(k.casefold())
                stack.append(item)
        elif isinstance(v, list):
            stack.extend(v)
    return out


def _pairs(text: str) -> list[tuple[str, str]]:
    return urllib.parse.parse_qsl(text, keep_blank_values=True, errors="strict")


def _query(r: Request) -> dict[str, str] | str:
    """The query parameters, or why they are refused (a name given twice)."""
    out: dict[str, str] = {}
    for k, v in _pairs(r.query):
        if k in out:
            return f"the query gives {k} more than once"
        out[k] = v
    return out


# -- query and JSON protocols: IAM, STS, Secrets Manager -----------------------


@dataclass(frozen=True)
class _Op:
    """One RPC operation: its effect, the member naming the ARN it acts on
    ("" for an action with no resource), what that ARN's resource part
    starts with, and members that make the request reach something else."""

    effect: str
    member: str = ""
    kinds: tuple[str, ...] = ()
    forbid: tuple[str, ...] = ()
    check: bool = True


_POLICY, _OIDC, _SAML, _MFA = ("policy/",), ("oidc-provider/",), ("saml-provider/",), ("mfa/", "sms-mfa/")
_PRINCIPAL = ("user/", "group/", "role/")

IAM: dict[str, _Op] = {
    **{
        op: _Op(effect, "PolicyArn", _POLICY)
        for op, effect in (
            ("CreatePolicyVersion", "write"),
            ("DeletePolicy", "destructive"),
            ("DeletePolicyVersion", "destructive"),
            ("SetDefaultPolicyVersion", "write"),
            ("TagPolicy", "write"),
            ("UntagPolicy", "write"),
            ("GetPolicy", "read"),
            ("GetPolicyVersion", "read"),
            ("ListPolicyVersions", "read"),
            ("ListEntitiesForPolicy", "read"),
            ("ListPolicyTags", "read"),
        )
    },
    **{
        op: _Op(effect, "OpenIDConnectProviderArn", _OIDC)
        for op, effect in (
            ("AddClientIDToOpenIDConnectProvider", "write"),
            ("RemoveClientIDFromOpenIDConnectProvider", "write"),
            ("UpdateOpenIDConnectProviderThumbprint", "write"),
            ("DeleteOpenIDConnectProvider", "destructive"),
            ("TagOpenIDConnectProvider", "write"),
            ("UntagOpenIDConnectProvider", "write"),
            ("GetOpenIDConnectProvider", "read"),
            ("ListOpenIDConnectProviderTags", "read"),
        )
    },
    **{
        op: _Op(effect, "SAMLProviderArn", _SAML)
        for op, effect in (
            ("UpdateSAMLProvider", "write"),
            ("DeleteSAMLProvider", "destructive"),
            ("TagSAMLProvider", "write"),
            ("UntagSAMLProvider", "write"),
            ("GetSAMLProvider", "read"),
            ("ListSAMLProviderTags", "read"),
        )
    },
    **{
        op: _Op(effect, "SerialNumber", _MFA)
        for op, effect in (("DeleteVirtualMFADevice", "destructive"), ("TagMFADevice", "write"), ("UntagMFADevice", "write"), ("ListMFADeviceTags", "read"))
    },
    "SimulatePrincipalPolicy": _Op("read", "PolicySourceArn", _PRINCIPAL),
    "GetContextKeysForPrincipalPolicy": _Op("read", "PolicySourceArn", _PRINCIPAL),
    "GenerateServiceLastAccessedDetails": _Op("read", "Arn", (*_PRINCIPAL, "policy/")),
    "ListPoliciesGrantingServiceAccess": _Op("read", "Arn", _PRINCIPAL),
    # IAM authorizes these with no resource.
    **{
        op: _Op(effect)
        for op, effect in (
            ("CreateAccountAlias", "write"),
            ("DeleteAccountAlias", "destructive"),
            ("UpdateAccountPasswordPolicy", "write"),
            ("DeleteAccountPasswordPolicy", "destructive"),
            ("SetSecurityTokenServicePreferences", "write"),
            ("EnableOutboundWebIdentityFederation", "write"),
            ("DisableOutboundWebIdentityFederation", "write"),
            ("GenerateCredentialReport", "read"),
            ("GetAccountAuthorizationDetails", "read"),
            ("GetAccountPasswordPolicy", "read"),
            ("GetAccountProperties", "read"),
            ("GetAccountSummary", "read"),
            ("GetContextKeysForCustomPolicy", "read"),
            ("GetCredentialReport", "read"),
            ("GetOrganizationsAccessReport", "read"),
            ("GetOutboundWebIdentityFederationInfo", "read"),
            ("GetServiceLastAccessedDetails", "read"),
            ("GetServiceLastAccessedDetailsWithEntities", "read"),
            ("ListAccountAliases", "read"),
            ("ListDelegationRequests", "read"),
            ("ListGroups", "read"),
            ("ListInstanceProfiles", "read"),
            ("ListOpenIDConnectProviders", "read"),
            ("ListPolicies", "read"),
            ("ListRoles", "read"),
            ("ListSAMLProviders", "read"),
            ("ListServerCertificates", "read"),
            ("ListUsers", "read"),
            ("ListVirtualMFADevices", "read"),
            ("SimulateCustomPolicy", "read"),
        )
    },
}

_BY_NAME = "names the IAM entity by name: its ARN carries the account id and the IAM path, which the request does not, so the resource cannot be built exactly"
_NEW = "creates an IAM entity whose ARN carries the account id, which the request does not"
IAM_UNROUTED: dict[str, str] = {
    **dict.fromkeys(
        (
            "AddRoleToInstanceProfile",
            "AddUserToGroup",
            "AttachGroupPolicy",
            "AttachRolePolicy",
            "AttachUserPolicy",
            "CreateAccessKey",
            "CreateLoginProfile",
            "CreateServiceSpecificCredential",
            "DeactivateMFADevice",
            "DeleteAccessKey",
            "DeleteGroup",
            "DeleteGroupPolicy",
            "DeleteInstanceProfile",
            "DeleteLoginProfile",
            "DeleteRole",
            "DeleteRolePermissionsBoundary",
            "DeleteRolePolicy",
            "DeleteSSHPublicKey",
            "DeleteServerCertificate",
            "DeleteServiceLinkedRole",
            "DeleteServiceSpecificCredential",
            "DeleteSigningCertificate",
            "DeleteUser",
            "DeleteUserPermissionsBoundary",
            "DeleteUserPolicy",
            "DetachGroupPolicy",
            "DetachRolePolicy",
            "DetachUserPolicy",
            "EnableMFADevice",
            "GetGroup",
            "GetGroupPolicy",
            "GetInstanceProfile",
            "GetLoginProfile",
            "GetMFADevice",
            "GetRole",
            "GetRolePolicy",
            "GetSSHPublicKey",
            "GetServerCertificate",
            "GetUser",
            "GetUserPolicy",
            "ListAccessKeys",
            "ListAttachedGroupPolicies",
            "ListAttachedRolePolicies",
            "ListAttachedUserPolicies",
            "ListGroupPolicies",
            "ListGroupsForUser",
            "ListInstanceProfileTags",
            "ListInstanceProfilesForRole",
            "ListMFADevices",
            "ListRolePolicies",
            "ListRoleTags",
            "ListSSHPublicKeys",
            "ListServerCertificateTags",
            "ListServiceSpecificCredentials",
            "ListSigningCertificates",
            "ListUserPolicies",
            "ListUserTags",
            "PutGroupPolicy",
            "PutRolePermissionsBoundary",
            "PutRolePolicy",
            "PutUserPermissionsBoundary",
            "PutUserPolicy",
            "RemoveRoleFromInstanceProfile",
            "RemoveUserFromGroup",
            "ResetServiceSpecificCredential",
            "ResyncMFADevice",
            "TagInstanceProfile",
            "TagRole",
            "TagServerCertificate",
            "TagUser",
            "UntagInstanceProfile",
            "UntagRole",
            "UntagServerCertificate",
            "UntagUser",
            "UpdateAccessKey",
            "UpdateAssumeRolePolicy",
            "UpdateGroup",
            "UpdateLoginProfile",
            "UpdateRole",
            "UpdateRoleDescription",
            "UpdateSSHPublicKey",
            "UpdateServerCertificate",
            "UpdateServiceSpecificCredential",
            "UpdateSigningCertificate",
            "UpdateUser",
            "UploadSSHPublicKey",
            "UploadSigningCertificate",
        ),
        _BY_NAME,
    ),
    **dict.fromkeys(
        (
            "CreateGroup",
            "CreateInstanceProfile",
            "CreateOpenIDConnectProvider",
            "CreatePolicy",
            "CreateRole",
            "CreateSAMLProvider",
            "CreateServiceLinkedRole",
            "CreateUser",
            "CreateVirtualMFADevice",
            "UploadServerCertificate",
            "GenerateOrganizationsAccessReport",
        ),
        _NEW,
    ),
    **dict.fromkeys(
        (
            "AcceptDelegationRequest",
            "AssociateDelegationRequest",
            "CreateDelegationRequest",
            "GetDelegationRequest",
            "GetHumanReadableSummary",
            "RejectDelegationRequest",
            "SendDelegationToken",
            "UpdateDelegationRequest",
        ),
        "delegation requests are named by id; their ARN carries the account id, which the request does not",
    ),
    **dict.fromkeys(
        (
            "DisableOrganizationsRootCredentialsManagement",
            "DisableOrganizationsRootSessions",
            "EnableOrganizationsRootCredentialsManagement",
            "EnableOrganizationsRootSessions",
            "ListOrganizationsFeatures",
        ),
        "an AWS Organizations setting, authorized through Organizations rather than an IAM action on this account",
    ),
    "AcquireRole": "creates or changes a role from a template, several IAM actions on a role named by name",
    "GetRoleTemplateVersion": "role templates are AWS-owned ARNs (account aws), which the aws integration does not check",
    "ChangePassword": "changes the calling IAM user's own password, the agent's",
    "GetAccessKeyLastUsed": "names an access key, not the user it belongs to",
    "GetServiceLinkedRoleDeletionStatus": "names a deletion task, not the role",
    "PutAccountProperties": "also needs iam:CreateServiceLinkedRole on a role the request does not name",
}

_SESSION_EXTRAS = ("Tags", "TransitiveTagKeys", "SourceIdentity", "ProvidedContexts")
STS: dict[str, _Op] = {
    "AssumeRole": _Op("write", "RoleArn", ("role/",), forbid=_SESSION_EXTRAS),
    "GetCallerIdentity": _Op("read", check=False),
    "DecodeAuthorizationMessage": _Op("read"),
    "GetAccessKeyInfo": _Op("read"),
    "GetWebIdentityToken": _Op("write", forbid=("Tags",)),
}
STS_UNROUTED: dict[str, str] = {
    "AssumeRoleWithSAML": "authorized by the role's trust policy and the SAML assertion, not by the caller's policies",
    "AssumeRoleWithWebIdentity": "authorized by the role's trust policy and the web identity token, not by the caller's policies",
    "AssumeRoot": "acts on another account's root user; the connection answers for its own account",
    "GetFederationToken": "the federated user's ARN carries the account id, which the request does not",
    "GetDelegatedAccessToken": "trades a delegation token the request does not tie to a resource",
    "GetSessionToken": "returns temporary credentials with the agent's own permissions, which IAM policies do not authorize",
}

_SECRET = ("secret:",)
_SECRET_ARN_RE = re.compile(r"arn:[a-z-]+:secretsmanager:[a-z0-9-]+:[0-9]{12}:secret:[A-Za-z0-9/_+=.@-]+-[A-Za-z0-9]{6}")
# A full ARN whose name, without the suffix, itself ends in a hyphen and six
# characters: Secrets Manager may read the whole of it as a partial ARN of
# another secret, so which secret it names is not certain.
_SECRET_AMBIGUOUS_RE = re.compile(r".*:secret:.*-[A-Za-z0-9]{6}-[A-Za-z0-9]{6}")
SECRETSMANAGER: dict[str, _Op] = {
    **{
        op: _Op(effect, "SecretId", _SECRET)
        for op, effect in (
            ("CancelRotateSecret", "write"),
            ("DeleteResourcePolicy", "destructive"),
            ("DeleteSecret", "destructive"),
            ("DescribeSecret", "read"),
            ("GetResourcePolicy", "read"),
            ("GetSecretValue", "read"),
            ("ListSecretVersionIds", "read"),
            ("PutResourcePolicy", "write"),
            ("PutSecretValue", "write"),
            ("RemoveRegionsFromReplication", "destructive"),
            ("RestoreSecret", "write"),
            ("StopReplicationToReplica", "write"),
            ("TagResource", "write"),
            ("UntagResource", "write"),
            ("UpdateSecretVersionStage", "write"),
        )
    },
    "UpdateSecret": _Op("write", "SecretId", _SECRET, forbid=("KmsKeyId",)),
    "ReplicateSecretToRegions": _Op("write", "SecretId", _SECRET, forbid=("KmsKeyId",)),
    "RotateSecret": _Op("write", "SecretId", _SECRET, forbid=("RotationLambdaARN", "ExternalSecretRotationRoleArn", "ExternalSecretRotationMetadata")),
    "ListSecrets": _Op("read"),
    "GetRandomPassword": _Op("read"),
}
SECRETSMANAGER_UNROUTED: dict[str, str] = {
    "BatchGetSecretValue": "reads several secrets, or the ones a filter selects",
    "CreateSecret": "the new secret's ARN carries the account id and a suffix AWS picks",
    "ValidateResourcePolicy": "authorized as both secretsmanager:PutResourcePolicy and secretsmanager:ValidateResourcePolicy",
}

_FORBID_REASONS = {
    "tags": "session or resource tags need a second action (TagSession, TagResource)",
    "transitivetagkeys": "session tags need sts:TagSession too",
    "sourceidentity": "a source identity needs sts:SetSourceIdentity too",
    "providedcontexts": "provided contexts need sts:SetContext too",
    "kmskeyid": "a KMS key named in the request is used with the agent's KMS permissions",
    "rotationlambdaarn": "a rotation function is a second resource the request reaches",
    "externalsecretrotationrolearn": "an external rotation role is passed with iam:PassRole",
    "externalsecretrotationmetadata": "external rotation reaches a partner service the request names",
}


def _rpc(service: str, operation: str, spec: _Op | None, unrouted: Mapping[str, str], fields: Mapping[str, Any], names: set[str], p: _Place) -> dict[str, str]:
    """One query or JSON operation: the ARN from its member, checked."""
    if spec is None:
        reason = unrouted.get(operation)
        return _unresolved(f"{service}:{operation} is not routed" + (f": {reason}" if reason else ""), operation)
    for f in spec.forbid:
        if f.casefold() in names:
            return _unresolved(f"{operation} with {f}: {_FORBID_REASONS[f.casefold()]}", operation)
    if not spec.check:
        return _unchecked(operation)
    if not spec.member:
        return _ok(service, operation, "all", spec.effect, operation, p.partition)
    value = fields.get(spec.member)
    if not isinstance(value, str) or not value:
        return _unresolved(f"{operation} names no {spec.member}", operation)
    if service == "secretsmanager":
        if _SECRET_AMBIGUOUS_RE.fullmatch(value):
            return _unresolved(
                f"{operation}: {spec.member} {value!r} could be a full ARN or a partial ARN of another secret "
                "(its name ends in a hyphen and six characters before the suffix); rename the secret",
                operation,
            )
        arn = _arn(value, p, "secretsmanager", spec.kinds, True) if _SECRET_ARN_RE.fullmatch(value) else None
        want = f"the secret's full ARN (arn:{p.partition}:secretsmanager:{p.region}:<account>:secret:<name>-<6 characters>)"
    else:
        arn = _arn(value, p, "iam", spec.kinds, False)
        want = f"an ARN of the {' or '.join(k.rstrip('/') for k in spec.kinds)} in {p.partition} (arn:{p.partition}:iam::<account>:...)"
    if arn is None:
        return _unresolved(f"{operation}: {spec.member} {value!r} must be {want}", operation)
    return _ok(service, operation, arn, spec.effect, operation, p.partition)


def _query_protocol(r: Request, p: _Place) -> dict[str, str]:
    """IAM and STS: Action and the members in the query string or a form body."""
    if r.body and r.content_type != "application/x-www-form-urlencoded":
        return _unresolved("a query-protocol request's body must be application/x-www-form-urlencoded")
    params: dict[str, str] = {}
    seen: set[str] = set()
    for k, v in _pairs(r.query) + _pairs(r.body.decode("utf-8") if r.body else ""):
        if k.casefold() in seen:
            return _unresolved(f"{k} is given more than once")
        seen.add(k.casefold())
        params[k] = v
    operation = params.get("Action", "")
    if not operation:
        return _unresolved("the request names no Action")
    names = {k.split(".", 1)[0].casefold() for k in params}
    table, unrouted = (IAM, IAM_UNROUTED) if p.service == "iam" else (STS, STS_UNROUTED)
    return _rpc(p.service, operation, table.get(operation), unrouted, params, names, p)


def _json_protocol(r: Request, p: _Place) -> dict[str, str]:
    """Secrets Manager: X-Amz-Target secretsmanager.<Op> and a JSON 1.1 body."""
    prefix, sep, operation = r.headers.get("x-amz-target", "").partition(".")
    if prefix != "secretsmanager" or not sep or not operation:
        return _unresolved("the request's X-Amz-Target is not secretsmanager.<Operation>")
    if r.content_type != "application/x-amz-json-1.1":
        return _unresolved("a Secrets Manager request's body must be application/x-amz-json-1.1", operation)
    try:
        doc = _json_body(r.body)
    except (ValueError, UnicodeDecodeError):
        return _unresolved("the body is not JSON", operation)
    if not isinstance(doc, _Obj):
        return _unresolved("the body is not a JSON object", operation)
    folded = [k.casefold() for k, _ in doc]
    if len(set(folded)) != len(folded):
        return _unresolved("the body gives a member more than once", operation)
    return _rpc("secretsmanager", operation, SECRETSMANAGER.get(operation), SECRETSMANAGER_UNROUTED, dict(doc), _names(doc), p)


# -- Lambda --------------------------------------------------------------------


@dataclass(frozen=True)
class _LambdaOp:
    """What a Lambda operation acts on: ``function`` (the {FunctionName}),
    ``version`` (a function version or alias: the qualifier is required),
    ``tags`` (any Lambda ARN in {Resource}), ``csc`` (a code signing config),
    ``layer``, ``layerversion``, ``layerarn`` (a layer version in ?Arn=),
    ``durable`` (a durable execution) or ``all`` (no resource)."""

    kind: str
    effect: str
    action: str = ""  # the IAM action, when it is not the operation's name
    qualifier: bool = False  # the operation takes a qualifier
    forbid: tuple[str, ...] = ()


LAMBDA_PATHS: tuple[tuple[str, str, str], ...] = (
    ("POST", "/2018-10-31/layers/{LayerName}/versions/{VersionNumber}/policy", "AddLayerVersionPermission"),
    ("POST", "/2015-03-31/functions/{FunctionName}/policy", "AddPermission"),
    ("POST", "/2025-12-01/durable-executions/{DurableExecutionArn}/checkpoint", "CheckpointDurableExecution"),
    ("POST", "/2015-03-31/functions/{FunctionName}/aliases", "CreateAlias"),
    ("POST", "/2025-11-30/capacity-providers", "CreateCapacityProvider"),
    ("POST", "/2020-04-22/code-signing-configs", "CreateCodeSigningConfig"),
    ("POST", "/2015-03-31/event-source-mappings", "CreateEventSourceMapping"),
    ("POST", "/2015-03-31/functions", "CreateFunction"),
    ("POST", "/2021-10-31/functions/{FunctionName}/url", "CreateFunctionUrlConfig"),
    ("DELETE", "/2015-03-31/functions/{FunctionName}/aliases/{Name}", "DeleteAlias"),
    ("DELETE", "/2025-11-30/capacity-providers/{CapacityProviderName}", "DeleteCapacityProvider"),
    ("DELETE", "/2020-04-22/code-signing-configs/{CodeSigningConfigArn}", "DeleteCodeSigningConfig"),
    ("DELETE", "/2015-03-31/event-source-mappings/{UUID}", "DeleteEventSourceMapping"),
    ("DELETE", "/2015-03-31/functions/{FunctionName}", "DeleteFunction"),
    ("DELETE", "/2020-06-30/functions/{FunctionName}/code-signing-config", "DeleteFunctionCodeSigningConfig"),
    ("DELETE", "/2017-10-31/functions/{FunctionName}/concurrency", "DeleteFunctionConcurrency"),
    ("DELETE", "/2019-09-25/functions/{FunctionName}/event-invoke-config", "DeleteFunctionEventInvokeConfig"),
    ("DELETE", "/2021-10-31/functions/{FunctionName}/url", "DeleteFunctionUrlConfig"),
    ("DELETE", "/2018-10-31/layers/{LayerName}/versions/{VersionNumber}", "DeleteLayerVersion"),
    ("DELETE", "/2019-09-30/functions/{FunctionName}/provisioned-concurrency", "DeleteProvisionedConcurrencyConfig"),
    ("DELETE", "/2026-07-09/resource-policy/{ResourceArn}", "DeleteResourcePolicy"),
    ("GET", "/2016-08-19/account-settings", "GetAccountSettings"),
    ("GET", "/2015-03-31/functions/{FunctionName}/aliases/{Name}", "GetAlias"),
    ("GET", "/2025-11-30/capacity-providers/{CapacityProviderName}", "GetCapacityProvider"),
    ("GET", "/2020-04-22/code-signing-configs/{CodeSigningConfigArn}", "GetCodeSigningConfig"),
    ("GET", "/2025-12-01/durable-executions/{DurableExecutionArn}", "GetDurableExecution"),
    ("GET", "/2025-12-01/durable-executions/{DurableExecutionArn}/history", "GetDurableExecutionHistory"),
    ("GET", "/2025-12-01/durable-executions/{DurableExecutionArn}/state", "GetDurableExecutionState"),
    ("GET", "/2015-03-31/event-source-mappings/{UUID}", "GetEventSourceMapping"),
    ("GET", "/2015-03-31/functions/{FunctionName}", "GetFunction"),
    ("GET", "/2020-06-30/functions/{FunctionName}/code-signing-config", "GetFunctionCodeSigningConfig"),
    ("GET", "/2019-09-30/functions/{FunctionName}/concurrency", "GetFunctionConcurrency"),
    ("GET", "/2015-03-31/functions/{FunctionName}/configuration", "GetFunctionConfiguration"),
    ("GET", "/2019-09-25/functions/{FunctionName}/event-invoke-config", "GetFunctionEventInvokeConfig"),
    ("GET", "/2024-08-31/functions/{FunctionName}/recursion-config", "GetFunctionRecursionConfig"),
    ("GET", "/2025-11-30/functions/{FunctionName}/function-scaling-config", "GetFunctionScalingConfig"),
    ("GET", "/2021-10-31/functions/{FunctionName}/url", "GetFunctionUrlConfig"),
    ("GET", "/2018-10-31/layers/{LayerName}/versions/{VersionNumber}", "GetLayerVersion"),
    ("GET", "/2018-10-31/layers/{LayerName}/versions/{VersionNumber}/policy", "GetLayerVersionPolicy"),
    ("GET", "/2015-03-31/functions/{FunctionName}/policy", "GetPolicy"),
    ("GET", "/2019-09-30/functions/{FunctionName}/provisioned-concurrency", "GetProvisionedConcurrencyConfig"),
    ("GET", "/2026-07-09/resource-policy/{ResourceArn}", "GetResourcePolicy"),
    ("GET", "/2021-07-20/functions/{FunctionName}/runtime-management-config", "GetRuntimeManagementConfig"),
    ("POST", "/2015-03-31/functions/{FunctionName}/invocations", "Invoke"),
    ("POST", "/2014-11-13/functions/{FunctionName}/invoke-async", "InvokeAsync"),
    ("POST", "/2021-11-15/functions/{FunctionName}/response-streaming-invocations", "InvokeWithResponseStream"),
    ("GET", "/2015-03-31/functions/{FunctionName}/aliases", "ListAliases"),
    ("GET", "/2025-11-30/capacity-providers", "ListCapacityProviders"),
    ("GET", "/2020-04-22/code-signing-configs", "ListCodeSigningConfigs"),
    ("GET", "/2025-12-01/functions/{FunctionName}/durable-executions", "ListDurableExecutionsByFunction"),
    ("GET", "/2015-03-31/event-source-mappings", "ListEventSourceMappings"),
    ("GET", "/2019-09-25/functions/{FunctionName}/event-invoke-config/list", "ListFunctionEventInvokeConfigs"),
    ("GET", "/2021-10-31/functions/{FunctionName}/urls", "ListFunctionUrlConfigs"),
    ("GET", "/2025-11-30/capacity-providers/{CapacityProviderName}/function-versions", "ListFunctionVersionsByCapacityProvider"),
    ("GET", "/2015-03-31/functions", "ListFunctions"),
    ("GET", "/2020-04-22/code-signing-configs/{CodeSigningConfigArn}/functions", "ListFunctionsByCodeSigningConfig"),
    ("GET", "/2018-10-31/layers/{LayerName}/versions", "ListLayerVersions"),
    ("GET", "/2018-10-31/layers", "ListLayers"),
    ("GET", "/2017-03-31/tags/{Resource}", "ListTags"),
    ("GET", "/2015-03-31/functions/{FunctionName}/versions", "ListVersionsByFunction"),
    ("POST", "/2018-10-31/layers/{LayerName}/versions", "PublishLayerVersion"),
    ("POST", "/2015-03-31/functions/{FunctionName}/versions", "PublishVersion"),
    ("PUT", "/2020-06-30/functions/{FunctionName}/code-signing-config", "PutFunctionCodeSigningConfig"),
    ("PUT", "/2017-10-31/functions/{FunctionName}/concurrency", "PutFunctionConcurrency"),
    ("PUT", "/2019-09-25/functions/{FunctionName}/event-invoke-config", "PutFunctionEventInvokeConfig"),
    ("PUT", "/2024-08-31/functions/{FunctionName}/recursion-config", "PutFunctionRecursionConfig"),
    ("PUT", "/2025-11-30/functions/{FunctionName}/function-scaling-config", "PutFunctionScalingConfig"),
    ("PUT", "/2019-09-30/functions/{FunctionName}/provisioned-concurrency", "PutProvisionedConcurrencyConfig"),
    ("PUT", "/2026-07-09/resource-policy/{ResourceArn}", "PutResourcePolicy"),
    ("PUT", "/2021-07-20/functions/{FunctionName}/runtime-management-config", "PutRuntimeManagementConfig"),
    ("DELETE", "/2018-10-31/layers/{LayerName}/versions/{VersionNumber}/policy/{StatementId}", "RemoveLayerVersionPermission"),
    ("DELETE", "/2015-03-31/functions/{FunctionName}/policy/{StatementId}", "RemovePermission"),
    ("POST", "/2025-12-01/durable-execution-callbacks/{CallbackId}/fail", "SendDurableExecutionCallbackFailure"),
    ("POST", "/2025-12-01/durable-execution-callbacks/{CallbackId}/heartbeat", "SendDurableExecutionCallbackHeartbeat"),
    ("POST", "/2025-12-01/durable-execution-callbacks/{CallbackId}/succeed", "SendDurableExecutionCallbackSuccess"),
    ("POST", "/2025-12-01/durable-executions/{DurableExecutionArn}/stop", "StopDurableExecution"),
    ("POST", "/2017-03-31/tags/{Resource}", "TagResource"),
    ("DELETE", "/2017-03-31/tags/{Resource}", "UntagResource"),
    ("PUT", "/2015-03-31/functions/{FunctionName}/aliases/{Name}", "UpdateAlias"),
    ("PUT", "/2025-11-30/capacity-providers/{CapacityProviderName}", "UpdateCapacityProvider"),
    ("PUT", "/2020-04-22/code-signing-configs/{CodeSigningConfigArn}", "UpdateCodeSigningConfig"),
    ("PUT", "/2015-03-31/event-source-mappings/{UUID}", "UpdateEventSourceMapping"),
    ("PUT", "/2015-03-31/functions/{FunctionName}/code", "UpdateFunctionCode"),
    ("PUT", "/2015-03-31/functions/{FunctionName}/configuration", "UpdateFunctionConfiguration"),
    ("POST", "/2019-09-25/functions/{FunctionName}/event-invoke-config", "UpdateFunctionEventInvokeConfig"),
    ("PUT", "/2021-10-31/functions/{FunctionName}/url", "UpdateFunctionUrlConfig"),
)

LAMBDA: dict[str, _LambdaOp] = {
    "AddLayerVersionPermission": _LambdaOp("layerversion", "write"),
    "AddPermission": _LambdaOp("function", "write", qualifier=True),
    "CheckpointDurableExecution": _LambdaOp("durable", "write"),
    "CreateAlias": _LambdaOp("function", "write"),
    "CreateCodeSigningConfig": _LambdaOp("all", "write", forbid=("Tags",)),
    "CreateFunctionUrlConfig": _LambdaOp("function", "write", qualifier=True),
    "DeleteAlias": _LambdaOp("function", "destructive"),
    "DeleteCodeSigningConfig": _LambdaOp("csc", "destructive"),
    "DeleteFunction": _LambdaOp("function", "destructive", qualifier=True),
    "DeleteFunctionCodeSigningConfig": _LambdaOp("function", "destructive"),
    "DeleteFunctionConcurrency": _LambdaOp("function", "destructive"),
    "DeleteFunctionEventInvokeConfig": _LambdaOp("function", "destructive", qualifier=True),
    "DeleteFunctionUrlConfig": _LambdaOp("function", "destructive", qualifier=True),
    "DeleteLayerVersion": _LambdaOp("layerversion", "destructive"),
    "DeleteProvisionedConcurrencyConfig": _LambdaOp("version", "destructive"),
    "GetAccountSettings": _LambdaOp("all", "read"),
    "GetAlias": _LambdaOp("function", "read"),
    "GetCodeSigningConfig": _LambdaOp("csc", "read"),
    "GetDurableExecution": _LambdaOp("durable", "read"),
    "GetDurableExecutionHistory": _LambdaOp("durable", "read"),
    "GetDurableExecutionState": _LambdaOp("durable", "read"),
    "GetFunction": _LambdaOp("function", "read", qualifier=True),
    "GetFunctionCodeSigningConfig": _LambdaOp("function", "read"),
    "GetFunctionConcurrency": _LambdaOp("function", "read"),
    "GetFunctionConfiguration": _LambdaOp("function", "read", qualifier=True),
    "GetFunctionEventInvokeConfig": _LambdaOp("function", "read", qualifier=True),
    "GetFunctionRecursionConfig": _LambdaOp("function", "read"),
    "GetFunctionScalingConfig": _LambdaOp("function", "read", qualifier=True),
    "GetFunctionUrlConfig": _LambdaOp("function", "read", qualifier=True),
    "GetLayerVersion": _LambdaOp("layerversion", "read"),
    "GetLayerVersionByArn": _LambdaOp("layerarn", "read", action="GetLayerVersion"),
    "GetLayerVersionPolicy": _LambdaOp("layerversion", "read"),
    "GetPolicy": _LambdaOp("function", "read", qualifier=True),
    "GetProvisionedConcurrencyConfig": _LambdaOp("version", "read"),
    "GetRuntimeManagementConfig": _LambdaOp("function", "read", qualifier=True),
    "Invoke": _LambdaOp("function", "write", action="InvokeFunction", qualifier=True),
    "InvokeAsync": _LambdaOp("function", "write"),
    "InvokeWithResponseStream": _LambdaOp("function", "write", action="InvokeFunction", qualifier=True),
    "ListAliases": _LambdaOp("function", "read"),
    "ListCapacityProviders": _LambdaOp("all", "read"),
    "ListCodeSigningConfigs": _LambdaOp("all", "read"),
    "ListDurableExecutionsByFunction": _LambdaOp("function", "read", qualifier=True),
    "ListEventSourceMappings": _LambdaOp("all", "read"),
    "ListFunctionEventInvokeConfigs": _LambdaOp("function", "read"),
    "ListFunctionUrlConfigs": _LambdaOp("function", "read"),
    "ListFunctions": _LambdaOp("all", "read"),
    "ListFunctionsByCodeSigningConfig": _LambdaOp("csc", "read"),
    "ListLayerVersions": _LambdaOp("all", "read"),
    "ListLayers": _LambdaOp("all", "read"),
    "ListProvisionedConcurrencyConfigs": _LambdaOp("function", "read"),
    "ListTags": _LambdaOp("tags", "read"),
    "ListVersionsByFunction": _LambdaOp("function", "read"),
    "PublishLayerVersion": _LambdaOp("layer", "write", forbid=("S3Bucket", "S3Key", "S3ObjectVersion")),
    "PublishVersion": _LambdaOp("function", "write"),
    "PutFunctionConcurrency": _LambdaOp("function", "write"),
    "PutFunctionEventInvokeConfig": _LambdaOp("function", "write", qualifier=True),
    "PutFunctionRecursionConfig": _LambdaOp("function", "write"),
    "PutFunctionScalingConfig": _LambdaOp("function", "write", qualifier=True),
    "PutProvisionedConcurrencyConfig": _LambdaOp("version", "write"),
    "PutRuntimeManagementConfig": _LambdaOp("function", "write", qualifier=True),
    "RemoveLayerVersionPermission": _LambdaOp("layerversion", "destructive"),
    "RemovePermission": _LambdaOp("function", "destructive", qualifier=True),
    "StopDurableExecution": _LambdaOp("durable", "write"),
    "TagResource": _LambdaOp("tags", "write"),
    "UntagResource": _LambdaOp("tags", "write"),
    "UpdateAlias": _LambdaOp("function", "write"),
    "UpdateCodeSigningConfig": _LambdaOp("csc", "write"),
    "UpdateFunctionCode": _LambdaOp("function", "write", forbid=("S3Bucket", "S3Key", "S3ObjectVersion", "ImageUri", "SourceKMSKeyArn")),
    "UpdateFunctionConfiguration": _LambdaOp("function", "write", forbid=("Role", "Layers", "KMSKeyArn", "CapacityProviderConfig")),
    "UpdateFunctionEventInvokeConfig": _LambdaOp("function", "write", qualifier=True),
    "UpdateFunctionUrlConfig": _LambdaOp("function", "write", qualifier=True),
}
_PASSES_ROLE = "passes an execution role (iam:PassRole on a role the request names by ARN in the body) besides the Lambda action"
LAMBDA_UNROUTED: dict[str, str] = {
    "CreateFunction": _PASSES_ROLE,
    "CreateCapacityProvider": _PASSES_ROLE,
    "CreateEventSourceMapping": "connects a function to a queue or stream named in the body; the mapping's ARN carries the account id",
    "DeleteEventSourceMapping": "names the mapping by id; its ARN carries the account id, which the request does not",
    "GetEventSourceMapping": "names the mapping by id; its ARN carries the account id, which the request does not",
    "UpdateEventSourceMapping": "names the mapping by id; its ARN carries the account id, which the request does not",
    "DeleteCapacityProvider": "names the capacity provider by name; its ARN carries the account id, which the request does not",
    "GetCapacityProvider": "names the capacity provider by name; its ARN carries the account id, which the request does not",
    "UpdateCapacityProvider": "names the capacity provider by name; its ARN carries the account id, which the request does not",
    "ListFunctionVersionsByCapacityProvider": "names the capacity provider by name; its ARN carries the account id, which the request does not",
    "PutFunctionCodeSigningConfig": "acts on two resources, the function and the code signing config named in the body",
    "DeleteResourcePolicy": "authorized as two actions, lambda:DeleteResourcePolicy and lambda:RemovePermission",
    "GetResourcePolicy": "authorized as two actions, lambda:GetResourcePolicy and lambda:GetPolicy",
    "PutResourcePolicy": "authorized as several actions (PutResourcePolicy, AddPermission, RemovePermission)",
    "SendDurableExecutionCallbackFailure": "names a callback by id, not the durable execution it belongs to",
    "SendDurableExecutionCallbackHeartbeat": "names a callback by id, not the durable execution it belongs to",
    "SendDurableExecutionCallbackSuccess": "names a callback by id, not the durable execution it belongs to",
}
_LAMBDA_TEMPLATES = [(PathTemplate(path), method, op) for method, path, op in LAMBDA_PATHS]
_FUNCTION_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
_QUALIFIER_RE = re.compile(r"[A-Za-z0-9$_-]{1,128}")
_LAMBDA_FORBID = {
    "s3bucket": "code from S3 is read with the agent's S3 permissions",
    "s3key": "code from S3 is read with the agent's S3 permissions",
    "s3objectversion": "code from S3 is read with the agent's S3 permissions",
    "imageuri": "an image is pulled with the agent's ECR permissions",
    "sourcekmskeyarn": "a KMS key named in the request is used with the agent's KMS permissions",
    "role": "a new execution role is passed with iam:PassRole",
    "layers": "layers are read with lambda:GetLayerVersion on each layer named",
    "kmskeyarn": "a KMS key named in the request is used with the agent's KMS permissions",
    "capacityproviderconfig": "a capacity provider is passed with lambda:PassCapacityProvider",
    "tags": "tags need lambda:TagResource too",
}


def _function_arn(name: str, qualifier: str, spec: _LambdaOp, op: str, p: _Place) -> str | dict[str, str]:
    """The function's ARN, qualified when the request names a version or alias."""
    parts = name.split(":")
    if name.startswith("arn:") and len(parts) in (7, 8):
        _, partition, service, region, account, kind, fn, *rest = parts
    elif len(parts) in (3, 4) and parts[1] == "function":
        partition, service, region = p.partition, "lambda", p.region
        account, kind, fn, *rest = parts
    else:
        return _unresolved(
            f"{op}: name the function by ARN (arn:{p.partition}:lambda:{p.region}:<account>:function:<name>); the account is not in the request", op
        )
    if (
        (partition, service, region, kind) != (p.partition, "lambda", p.region, "function")
        or not _ACCOUNT_RE.fullmatch(account)
        or not _FUNCTION_RE.fullmatch(fn)
    ):
        return _unresolved(f"{op}: {name!r} is not a function ARN in {p.partition} {p.region}", op)
    inline = rest[0] if rest else ""
    if inline and qualifier:
        return _unresolved(f"{op}: the function is qualified both in its name and in ?Qualifier=", op)
    q = inline or qualifier
    if q and not _QUALIFIER_RE.fullmatch(q):
        return _unresolved(f"{op}: {q!r} is not a version or alias", op)
    if q and not (spec.qualifier or spec.kind == "version"):
        return _unresolved(f"{op} takes no qualifier", op)
    if spec.kind == "version" and not q:
        return _unresolved(f"{op} needs the version or alias (?Qualifier=)", op)
    return f"arn:{p.partition}:lambda:{p.region}:{account}:function:{fn}" + (f":{q}" if q else "")


def _lambda_arn(value: str, p: _Place, kinds: Sequence[str], op: str, what: str) -> str | dict[str, str]:
    arn = _arn(value, p, "lambda", kinds, True)
    if arn is None:
        return _unresolved(f"{op}: {value!r} must be the {what}'s ARN in {p.partition} {p.region}", op)
    return arn


def _lambda(r: Request, p: _Place) -> dict[str, str]:
    method = r.method.upper()
    found = next(
        (
            (tpl, m, op, v)
            for tpl, v in matching_templates((t for t, _, _ in _LAMBDA_TEMPLATES), r.raw_path)
            for t, m, op in _LAMBDA_TEMPLATES
            if t is tpl and m == method
        ),
        None,
    )
    if found is None:
        return _unresolved(f"{method} {r.raw_path} is not a Lambda operation the table knows")
    _, _, op, raw_vars = found
    vars_: dict[str, str] = {}
    for k, v in raw_vars.items():
        vars_[k] = urllib.parse.unquote(v, errors="strict")
        if "/" in vars_[k] and k not in ("Resource", "DurableExecutionArn"):
            return _unresolved(f"{op}: {k} must not contain /", op)
    params = _query(r)
    if isinstance(params, str):
        return _unresolved(params, op)
    if op == "ListLayers" and "find" in params:
        if params["find"] != "LayerVersion":
            return _unresolved(f"ListLayers with find={params['find']!r} is not an operation the table knows", op)
        op = "GetLayerVersionByArn"
    if op == "GetProvisionedConcurrencyConfig" and "List" in params:
        if params["List"] != "ALL":
            return _unresolved(f"provisioned concurrency with List={params['List']!r} is not an operation the table knows", op)
        op = "ListProvisionedConcurrencyConfigs"
    spec = LAMBDA.get(op)
    if spec is None:
        return _unresolved(f"lambda:{op} is not routed: {LAMBDA_UNROUTED.get(op, 'not described')}", op)
    if spec.forbid and r.body:
        try:
            names = _names(_json_body(r.body))
        except (ValueError, UnicodeDecodeError):
            return _unresolved(f"{op}: the body is not JSON", op)
        for f in spec.forbid:
            if f.casefold() in names:
                return _unresolved(f"{op} with {f}: {_LAMBDA_FORBID[f.casefold()]}", op)
    action = spec.action or op
    if spec.kind == "all":
        return _ok("lambda", action, "all", spec.effect, op, p.partition)
    arn: str | dict[str, str]
    if spec.kind in ("function", "version"):
        arn = _function_arn(vars_["FunctionName"], params.get("Qualifier", ""), spec, op, p)
    elif spec.kind == "tags":
        arn = _lambda_arn(vars_["Resource"], p, ("function:", "code-signing-config:", "event-source-mapping:", "capacity-provider:"), op, "resource")
    elif spec.kind == "csc":
        arn = _lambda_arn(vars_["CodeSigningConfigArn"], p, ("code-signing-config:",), op, "code signing config")
    elif spec.kind == "durable":
        arn = _lambda_arn(vars_["DurableExecutionArn"], p, ("function:",), op, "durable execution")
        if isinstance(arn, str) and "/durable-execution/" not in arn:
            arn = _unresolved(f"{op}: {arn!r} is not a durable execution ARN", op)
    elif spec.kind == "layerarn":
        arn = _lambda_arn(params.get("Arn", ""), p, ("layer:",), op, "layer version")
        if isinstance(arn, str) and not re.fullmatch(r".*:layer:[A-Za-z0-9_-]+:[0-9]+", arn):
            arn = _unresolved(f"{op}: {arn!r} is not a layer version ARN", op)
    else:
        arn = _lambda_arn(vars_["LayerName"], p, ("layer:",), op, "layer")
        if isinstance(arn, str) and not re.fullmatch(r".*:layer:[A-Za-z0-9_-]+", arn):
            arn = _unresolved(f"{op}: {arn!r} is not an unversioned layer ARN", op)
        if isinstance(arn, str) and spec.kind == "layerversion":
            version = vars_["VersionNumber"]
            arn = f"{arn}:{version}" if version.isdigit() and version.isascii() else _unresolved(f"{op}: {version!r} is not a layer version", op)
    if isinstance(arn, dict):
        return arn
    return _ok("lambda", action, arn, spec.effect, op, p.partition)


# -- S3 ------------------------------------------------------------------------

_BUCKET_RE = re.compile(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]")
# Query parameters that select nothing: listings, paging, response overrides.
_S3_PARAMS = frozenset(
    [
        "prefix",
        "delimiter",
        "marker",
        "max-keys",
        "list-type",
        "continuation-token",
        "fetch-owner",
        "start-after",
        "encoding-type",
        "key-marker",
        "version-id-marker",
        "upload-id-marker",
        "max-uploads",
        "max-parts",
        "part-number-marker",
        "max-buckets",
        "bucket-region",
        "id",
        "select-type",
        "versionId",
        "uploadId",
        "partNumber",
        "x-id",
        "annotationName",
    ]
)
_S3_SUBRESOURCES = frozenset(
    [
        "acl",
        "policy",
        "policyStatus",
        "tagging",
        "versioning",
        "lifecycle",
        "cors",
        "website",
        "logging",
        "notification",
        "replication",
        "accelerate",
        "requestPayment",
        "location",
        "object-lock",
        "intelligent-tiering",
        "inventory",
        "analytics",
        "metrics",
        "encryption",
        "publicAccessBlock",
        "ownershipControls",
        "versions",
        "uploads",
        "delete",
        "restore",
        "select",
        "attributes",
        "legal-hold",
        "retention",
        "torrent",
        "session",
        "abac",
        "renameObject",
        "annotation",
        "metadataTable",
        "metadataConfiguration",
        "metadataInventoryTable",
        "metadataJournalTable",
        "metadataAnnotationTable",
    ]
)
# A bucket subresource: the operation for GET, PUT and DELETE (with ?id= for the
# configurations kept by id; GET without one lists them).
_BUCKET_SUBS: dict[str, tuple[str, str, str]] = {
    "acl": ("GetBucketAcl", "PutBucketAcl", ""),
    "policy": ("GetBucketPolicy", "PutBucketPolicy", "DeleteBucketPolicy"),
    "policyStatus": ("GetBucketPolicyStatus", "", ""),
    "tagging": ("GetBucketTagging", "PutBucketTagging", "DeleteBucketTagging"),
    "versioning": ("GetBucketVersioning", "PutBucketVersioning", ""),
    "lifecycle": ("GetBucketLifecycleConfiguration", "PutBucketLifecycleConfiguration", "DeleteBucketLifecycle"),
    "cors": ("GetBucketCors", "PutBucketCors", "DeleteBucketCors"),
    "website": ("GetBucketWebsite", "PutBucketWebsite", "DeleteBucketWebsite"),
    "logging": ("GetBucketLogging", "PutBucketLogging", ""),
    "notification": ("GetBucketNotificationConfiguration", "PutBucketNotificationConfiguration", ""),
    "replication": ("GetBucketReplication", "PutBucketReplication", "DeleteBucketReplication"),
    "accelerate": ("GetBucketAccelerateConfiguration", "PutBucketAccelerateConfiguration", ""),
    "requestPayment": ("GetBucketRequestPayment", "PutBucketRequestPayment", ""),
    "location": ("GetBucketLocation", "", ""),
    "object-lock": ("GetObjectLockConfiguration", "PutObjectLockConfiguration", ""),
    "encryption": ("GetBucketEncryption", "PutBucketEncryption", "DeleteBucketEncryption"),
    "publicAccessBlock": ("GetPublicAccessBlock", "PutPublicAccessBlock", "DeletePublicAccessBlock"),
    "ownershipControls": ("GetBucketOwnershipControls", "PutBucketOwnershipControls", "DeleteBucketOwnershipControls"),
    "intelligent-tiering": (
        "GetBucketIntelligentTieringConfiguration",
        "PutBucketIntelligentTieringConfiguration",
        "DeleteBucketIntelligentTieringConfiguration",
    ),
    "inventory": ("GetBucketInventoryConfiguration", "PutBucketInventoryConfiguration", "DeleteBucketInventoryConfiguration"),
    "analytics": ("GetBucketAnalyticsConfiguration", "PutBucketAnalyticsConfiguration", "DeleteBucketAnalyticsConfiguration"),
    "metrics": ("GetBucketMetricsConfiguration", "PutBucketMetricsConfiguration", "DeleteBucketMetricsConfiguration"),
    "versions": ("ListObjectVersions", "", ""),
    "uploads": ("ListMultipartUploads", "", ""),
    "session": ("CreateSession", "", ""),
    "abac": ("GetBucketAbac", "PutBucketAbac", ""),
    "metadataTable": ("GetBucketMetadataTableConfiguration", "", "DeleteBucketMetadataTableConfiguration"),
    "metadataConfiguration": ("GetBucketMetadataConfiguration", "", "DeleteBucketMetadataConfiguration"),
    "metadataInventoryTable": ("", "UpdateBucketMetadataInventoryTableConfiguration", ""),
    "metadataJournalTable": ("", "UpdateBucketMetadataJournalTableConfiguration", ""),
    "metadataAnnotationTable": ("", "UpdateBucketMetadataAnnotationTableConfiguration", ""),
}
_LISTED_BY_ID = {
    "GetBucketIntelligentTieringConfiguration": "ListBucketIntelligentTieringConfigurations",
    "GetBucketInventoryConfiguration": "ListBucketInventoryConfigurations",
    "GetBucketAnalyticsConfiguration": "ListBucketAnalyticsConfigurations",
    "GetBucketMetricsConfiguration": "ListBucketMetricsConfigurations",
}
# An object subresource: the operation for GET, PUT, POST and DELETE.
_OBJECT_SUBS: dict[str, tuple[str, str, str, str]] = {
    "acl": ("GetObjectAcl", "PutObjectAcl", "", ""),
    "tagging": ("GetObjectTagging", "PutObjectTagging", "", "DeleteObjectTagging"),
    "legal-hold": ("GetObjectLegalHold", "PutObjectLegalHold", "", ""),
    "retention": ("GetObjectRetention", "PutObjectRetention", "", ""),
    "attributes": ("GetObjectAttributes", "", "", ""),
    "torrent": ("GetObjectTorrent", "", "", ""),
    "uploads": ("", "", "CreateMultipartUpload", ""),
    "restore": ("", "", "RestoreObject", ""),
    "select": ("", "", "SelectObjectContent", ""),
    "renameObject": ("", "RenameObject", "", ""),
    "encryption": ("", "UpdateObjectEncryption", "", ""),
    "annotation": ("GetObjectAnnotation", "PutObjectAnnotation", "", "DeleteObjectAnnotation"),
}

# The IAM action S3 authorizes each routed operation as, and its effect.
# Object operations act on arn:aws:s3:::<bucket>/<key>, the rest on the bucket.
S3: dict[str, tuple[str, str]] = {
    "ListBuckets": ("ListAllMyBuckets", "read"),
    "HeadBucket": ("ListBucket", "read"),
    "ListObjects": ("ListBucket", "read"),
    "ListObjectsV2": ("ListBucket", "read"),
    "ListObjectVersions": ("ListBucketVersions", "read"),
    "ListMultipartUploads": ("ListBucketMultipartUploads", "read"),
    "CreateBucket": ("CreateBucket", "write"),
    "DeleteBucket": ("DeleteBucket", "destructive"),
    "GetBucketAcl": ("GetBucketAcl", "read"),
    "PutBucketAcl": ("PutBucketAcl", "write"),
    "GetBucketPolicy": ("GetBucketPolicy", "read"),
    "PutBucketPolicy": ("PutBucketPolicy", "write"),
    "DeleteBucketPolicy": ("DeleteBucketPolicy", "destructive"),
    "GetBucketPolicyStatus": ("GetBucketPolicyStatus", "read"),
    "GetBucketTagging": ("GetBucketTagging", "read"),
    "PutBucketTagging": ("PutBucketTagging", "write"),
    "DeleteBucketTagging": ("PutBucketTagging", "destructive"),
    "GetBucketVersioning": ("GetBucketVersioning", "read"),
    "PutBucketVersioning": ("PutBucketVersioning", "write"),
    "GetBucketLifecycleConfiguration": ("GetLifecycleConfiguration", "read"),
    "PutBucketLifecycleConfiguration": ("PutLifecycleConfiguration", "write"),
    "DeleteBucketLifecycle": ("PutLifecycleConfiguration", "destructive"),
    "GetBucketCors": ("GetBucketCORS", "read"),
    "PutBucketCors": ("PutBucketCORS", "write"),
    "DeleteBucketCors": ("PutBucketCORS", "destructive"),
    "GetBucketWebsite": ("GetBucketWebsite", "read"),
    "PutBucketWebsite": ("PutBucketWebsite", "write"),
    "DeleteBucketWebsite": ("DeleteBucketWebsite", "destructive"),
    "GetBucketLogging": ("GetBucketLogging", "read"),
    "PutBucketLogging": ("PutBucketLogging", "write"),
    "GetBucketNotificationConfiguration": ("GetBucketNotification", "read"),
    "PutBucketNotificationConfiguration": ("PutBucketNotification", "write"),
    "GetBucketReplication": ("GetReplicationConfiguration", "read"),
    "DeleteBucketReplication": ("PutReplicationConfiguration", "destructive"),
    "GetBucketAccelerateConfiguration": ("GetAccelerateConfiguration", "read"),
    "PutBucketAccelerateConfiguration": ("PutAccelerateConfiguration", "write"),
    "GetBucketRequestPayment": ("GetBucketRequestPayment", "read"),
    "PutBucketRequestPayment": ("PutBucketRequestPayment", "write"),
    "GetBucketLocation": ("GetBucketLocation", "read"),
    "GetObjectLockConfiguration": ("GetBucketObjectLockConfiguration", "read"),
    "PutObjectLockConfiguration": ("PutBucketObjectLockConfiguration", "write"),
    "GetBucketEncryption": ("GetEncryptionConfiguration", "read"),
    "PutBucketEncryption": ("PutEncryptionConfiguration", "write"),
    "DeleteBucketEncryption": ("PutEncryptionConfiguration", "destructive"),
    "GetPublicAccessBlock": ("GetBucketPublicAccessBlock", "read"),
    "PutPublicAccessBlock": ("PutBucketPublicAccessBlock", "write"),
    "DeletePublicAccessBlock": ("PutBucketPublicAccessBlock", "destructive"),
    "GetBucketOwnershipControls": ("GetBucketOwnershipControls", "read"),
    "PutBucketOwnershipControls": ("PutBucketOwnershipControls", "write"),
    "DeleteBucketOwnershipControls": ("PutBucketOwnershipControls", "destructive"),
    **{
        f"{verb}Bucket{kind}Configuration{'s' if verb == 'List' else ''}": (
            f"{'Get' if verb == 'List' else verb.replace('Delete', 'Put')}{kind}Configuration",
            effect,
        )
        for kind in ("IntelligentTiering", "Inventory", "Analytics", "Metrics")
        for verb, effect in (("Get", "read"), ("List", "read"), ("Put", "write"), ("Delete", "destructive"))
    },
    "GetObject": ("GetObject", "read"),
    "HeadObject": ("GetObject", "read"),
    "GetObjectAttributes": ("GetObject", "read"),
    "GetObjectTorrent": ("GetObject", "read"),
    "SelectObjectContent": ("GetObject", "read"),
    "PutObject": ("PutObject", "write"),
    "CreateMultipartUpload": ("PutObject", "write"),
    "UploadPart": ("PutObject", "write"),
    "CompleteMultipartUpload": ("PutObject", "write"),
    "AbortMultipartUpload": ("AbortMultipartUpload", "destructive"),
    "ListParts": ("ListMultipartUploadParts", "read"),
    "DeleteObject": ("DeleteObject", "destructive"),
    "GetObjectAcl": ("GetObjectAcl", "read"),
    "PutObjectAcl": ("PutObjectAcl", "write"),
    "GetObjectTagging": ("GetObjectTagging", "read"),
    "PutObjectTagging": ("PutObjectTagging", "write"),
    "DeleteObjectTagging": ("DeleteObjectTagging", "destructive"),
    "GetObjectLegalHold": ("GetObjectLegalHold", "read"),
    "PutObjectLegalHold": ("PutObjectLegalHold", "write"),
    "GetObjectRetention": ("GetObjectRetention", "read"),
    "PutObjectRetention": ("PutObjectRetention", "write"),
    "RestoreObject": ("RestoreObject", "write"),
}
# With ?versionId= S3 authorizes the version's own action.
_S3_VERSIONED = {
    "GetObject": "GetObjectVersion",
    "HeadObject": "GetObjectVersion",
    "GetObjectAttributes": "GetObjectVersion",
    "DeleteObject": "DeleteObjectVersion",
    "GetObjectAcl": "GetObjectVersionAcl",
    "PutObjectAcl": "PutObjectVersionAcl",
    "GetObjectTagging": "GetObjectVersionTagging",
    "PutObjectTagging": "PutObjectVersionTagging",
    "DeleteObjectTagging": "DeleteObjectVersionTagging",
}
_S3_TABLES = "S3 Metadata tables are created in S3 Tables, with s3tables actions on resources the request does not name"
_S3_ANNOTATION = "object annotations are not described in the IAM service authorization reference"
S3_UNROUTED: dict[str, str] = {
    "CopyObject": "a copy reads the source object named in x-amz-copy-source, a second resource",
    "UploadPartCopy": "a copy reads the source object named in x-amz-copy-source, a second resource",
    "RenameObject": "a rename changes the source object named in x-amz-rename-source, a second resource",
    "DeleteObjects": "deletes every object the body lists",
    "PutBucketReplication": "passes the replication role in the body (iam:PutReplicationConfiguration and iam:PassRole)",
    "CreateSession": "S3 Express directory bucket sessions are not covered",
    "ListDirectoryBuckets": "S3 Express directory buckets are served by other hosts",
    "WriteGetObjectResponse": "S3 Object Lambda responses are served by other hosts",
    "UpdateObjectEncryption": "not described in the IAM service authorization reference",
    "GetBucketAbac": "not described in the IAM service authorization reference",
    "PutBucketAbac": "not described in the IAM service authorization reference",
    "GetObjectAnnotation": _S3_ANNOTATION,
    "PutObjectAnnotation": _S3_ANNOTATION,
    "DeleteObjectAnnotation": _S3_ANNOTATION,
    "ListObjectAnnotations": _S3_ANNOTATION,
    "CreateBucketMetadataTableConfiguration": _S3_TABLES,
    "CreateBucketMetadataConfiguration": _S3_TABLES,
    "UpdateBucketMetadataInventoryTableConfiguration": _S3_TABLES,
    "UpdateBucketMetadataJournalTableConfiguration": _S3_TABLES,
    "UpdateBucketMetadataAnnotationTableConfiguration": _S3_TABLES,
    "GetBucketMetadataTableConfiguration": _S3_TABLES,
    "GetBucketMetadataConfiguration": _S3_TABLES,
    "DeleteBucketMetadataTableConfiguration": _S3_TABLES,
    "DeleteBucketMetadataConfiguration": _S3_TABLES,
}
# Request headers that make S3 authorize a second action besides the
# operation's own (an ACL, tags, an object lock), or use a second resource.
_S3_EXTRA_HEADERS = {
    "x-amz-acl": "an ACL needs s3:PutObjectAcl or s3:PutBucketAcl too",
    "x-amz-tagging": "tags need s3:PutObjectTagging too",
    "x-amz-server-side-encryption-aws-kms-key-id": "a KMS key named in the request is used with the agent's KMS permissions",
    "x-amz-bypass-governance-retention": "bypassing governance retention needs s3:BypassGovernanceRetention too",
    "x-amz-bucket-object-lock-enabled": "object lock needs s3:PutBucketObjectLockConfiguration and s3:PutBucketVersioning too",
    "x-amz-object-ownership": "object ownership needs s3:PutBucketOwnershipControls too",
    "x-amz-copy-source": "a copy reads a second object",
    "x-amz-rename-source": "a rename changes a second object",
}
_S3_EXTRA_PREFIXES = {
    "x-amz-grant-": "an ACL grant needs s3:PutObjectAcl or s3:PutBucketAcl too",
    "x-amz-object-lock-": "an object lock needs s3:PutObjectRetention or s3:PutObjectLegalHold too",
}
# The SigV4 query parameters of a presigned URL, which select nothing. S3
# reads any other x-amz-* query parameter as the header of that name.
_S3_SIGV4_PARAMS = frozenset(
    ["x-amz-algorithm", "x-amz-credential", "x-amz-date", "x-amz-expires", "x-amz-signedheaders", "x-amz-signature", "x-amz-security-token"]
)


def _s3_extra(op: str, name: str) -> str | None:
    """Why the header ``name`` (lowercased) makes S3 authorize more than ``op``, or None."""
    if op in ("PutBucketAcl", "PutObjectAcl") and (name == "x-amz-acl" or name.startswith("x-amz-grant-")):
        return None  # the ACL is what these operations set
    return _S3_EXTRA_HEADERS.get(name) or next((why for pre, why in _S3_EXTRA_PREFIXES.items() if name.startswith(pre)), None)


def _s3_operation(method: str, key: str, sub: str, params: Mapping[str, str], headers: Collection[str]) -> str:
    """The S3 operation a request is, by method, level, subresource and the
    headers that tell the operations on one path apart; "" when none."""
    if key:
        get, put, post, delete = _OBJECT_SUBS.get(sub, ("", "", "", "")) if sub else ("", "", "", "")
        if sub:
            if sub == "annotation" and method == "GET" and "annotationName" not in params:
                return "ListObjectAnnotations"
            return {"GET": get, "PUT": put, "POST": post, "DELETE": delete}.get(method, "")
        uploading = "uploadId" in params
        copying = "x-amz-copy-source" in headers
        return {
            "GET": "ListParts" if uploading else "GetObject",
            "HEAD": "HeadObject",
            "PUT": ("UploadPartCopy" if uploading else "CopyObject") if copying else ("UploadPart" if uploading else "PutObject"),
            "POST": "CompleteMultipartUpload" if uploading else "",
            "DELETE": "AbortMultipartUpload" if uploading else "DeleteObject",
        }.get(method, "")
    if sub:
        if sub == "delete":
            return "DeleteObjects" if method == "POST" else ""
        if sub in ("metadataTable", "metadataConfiguration") and method == "POST":
            return "CreateBucketMetadataTableConfiguration" if sub == "metadataTable" else "CreateBucketMetadataConfiguration"
        get, put, delete = _BUCKET_SUBS.get(sub, ("", "", ""))
        if method == "GET" and get in _LISTED_BY_ID and "id" not in params:
            return _LISTED_BY_ID[get]
        return {"GET": get, "PUT": put, "DELETE": delete}.get(method, "")
    if method == "GET":
        return "ListObjectsV2" if params.get("list-type") == "2" else ("ListObjects" if "list-type" not in params else "")
    return {"HEAD": "HeadBucket", "PUT": "CreateBucket", "DELETE": "DeleteBucket"}.get(method, "")


def _s3(r: Request, p: _Place) -> dict[str, str]:
    method = r.method.upper()
    raw = r.raw_path
    if raw == "/":
        if method == "GET":
            return _ok("s3", "ListAllMyBuckets", "all", "read", "ListBuckets", p.partition)
        return _unresolved(f"{method} / is not an S3 operation")
    bucket, _, raw_key = raw[1:].partition("/")
    if "%" in bucket or not _BUCKET_RE.fullmatch(bucket):
        return _unresolved(f"{bucket!r} is not a bucket name (only path-style requests to lowercase, DNS-compatible names are routed)")
    key = urllib.parse.unquote(raw_key, errors="strict")
    if key and (any(c < " " or c == "\x7f" for c in key) or "?" in key or any(s in (".", "..") for s in key.split("/"))):
        return _unresolved("the object key has a control character, a ? or a . or .. segment, which cannot be checked exactly")
    params = _query(r)
    if isinstance(params, str):
        return _unresolved(params)
    subs = []
    amz: dict[str, str] = {}  # x-amz-* query parameters S3 reads as headers, lowercased
    for k in params:
        low = k.lower()
        if k in _S3_SUBRESOURCES:
            subs.append(k)
        elif low.startswith("x-amz-"):
            if low not in _S3_SIGV4_PARAMS:
                amz[low] = k
        elif k not in _S3_PARAMS and not k.startswith("response-"):
            return _unresolved(f"the S3 query parameter {k!r} is not one the table knows")
    if len(subs) > 1:
        return _unresolved(f"the request names several S3 subresources ({', '.join(sorted(subs))})")
    sub = subs[0] if subs else ""
    op = _s3_operation(method, key, sub, params, {*r.headers, *amz})
    if not op:
        return _unresolved(f"{method} on {'an object' if key else 'a bucket'}{' with ?' + sub if sub else ''} is not an S3 operation the table knows")
    routed = S3.get(op)
    if routed is None:
        return _unresolved(f"s3:{op} is not routed: {S3_UNROUTED.get(op, 'not described')}", op)
    for h in r.headers:
        reason = _s3_extra(op, h)
        if reason is not None:
            return _unresolved(f"{op} with {h}: {reason}", op)
    for low, k in amz.items():
        reason = _s3_extra(op, low)
        if reason is not None:
            return _unresolved(f"{op} with ?{k}= (S3 reads it as the {low} header): {reason}", op)
    if op == "RestoreObject" and r.body and (b"OutputLocation" in r.body or b"SelectParameters" in r.body):
        return _unresolved("a restore with an output location writes to a second bucket", op)
    action, effect = routed
    if "versionId" in params:
        action = _S3_VERSIONED.get(op, action)
    arn = f"arn:{p.partition}:s3:::{bucket}" + (f"/{key}" if key else "")
    if len(arn.encode()) > 1024:
        return _unresolved("the object's ARN is longer than a toolpass resource may be (1024 bytes)", op)
    return _ok("s3", action, arn, effect, op, p.partition)


# -- the table -----------------------------------------------------------------


def _aws(r: Request, vars_: Mapping[str, str]) -> dict[str, str]:
    p = place(r.host)
    if p is None:
        return _unresolved(f"{r.host} is not a host of a service the aws routes cover")
    if "x-http-method-override" in r.headers or "x-http-method" in r.headers:
        return _unresolved("a method override header is not accepted")
    if p.service == "s3":
        return _s3(r, p)
    if p.service == "lambda":
        return _lambda(r, p)
    if p.service == "secretsmanager":
        return _json_protocol(r, p)
    return _query_protocol(r, p)


_METHODS = ("GET", "HEAD", "PUT", "POST", "DELETE", "PATCH")
_INVOKE = (
    "/2015-03-31/functions/{FunctionName}/invocations",
    "/2021-11-15/functions/{FunctionName}/response-streaming-invocations",
    "/2014-11-13/functions/{FunctionName}/invoke-async",
)

ROUTES = RouteTable(
    "aws",
    [
        *(
            Route(m, path, "raw", "", body=_aws, sends_out=m == "PUT", description="the service from the host, the operation from the request", spec=False)
            for m in _METHODS
            for path in ("/", "/**")
        ),
        *(Route("POST", path, "raw", "", body=_aws, sends_out=True, untrusted_output=True, description="a Lambda invocation", spec=False) for path in _INVOKE),
    ],
    locate=_locate,
)

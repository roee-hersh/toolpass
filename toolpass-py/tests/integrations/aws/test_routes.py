"""The aws route table: requests to S3, Lambda, Secrets Manager, STS and IAM
mapped to IAM actions on ARNs, against the botocore service models."""

from __future__ import annotations

import json
import os
import re
import urllib.parse
from pathlib import Path
from typing import Any

import pytest

from toolpass.core.catalog import parse_resource as parse_ref
from toolpass.core.integration import Settings, find_action
from toolpass.core.routes import Request
from toolpass.integrations.aws import INTEGRATION
from toolpass.integrations.aws.actions import parse_resource
from toolpass.integrations.aws.routes import (
    IAM,
    IAM_UNROUTED,
    LAMBDA,
    LAMBDA_PATHS,
    LAMBDA_UNROUTED,
    ROUTES,
    S3,
    S3_UNROUTED,
    SECRETSMANAGER,
    SECRETSMANAGER_UNROUTED,
    STS,
    STS_UNROUTED,
    place,
)

ACCOUNT = "123456789012"
FN = f"arn:aws:lambda:eu-west-1:{ACCOUNT}:function:report"
SECRET = f"arn:aws:secretsmanager:eu-west-1:{ACCOUNT}:secret:prod/db-AbCdEf"
FORM = {"content-type": "application/x-www-form-urlencoded"}
SM = "application/x-amz-json-1.1"


def req(host: str, method: str, path: str, query: str = "", headers: dict[str, str] | None = None, body: bytes | None = None) -> Request:
    return Request("https", host, 443, method, path, query, headers or {}, body)


def s3(method: str, path: str, query: str = "", headers: dict[str, str] | None = None, body: bytes | None = None) -> Request:
    return req("s3.eu-west-1.amazonaws.com", method, path, query, headers, body)


def lam(method: str, path: str, query: str = "", body: bytes | None = None) -> Request:
    return req("lambda.eu-west-1.amazonaws.com", method, path, query, {"content-type": "application/json"}, body)


def iam(params: dict[str, str]) -> Request:
    return req("iam.amazonaws.com", "POST", "/", "", FORM, urllib.parse.urlencode({**params, "Version": "2010-05-08"}).encode())


def sts(params: dict[str, str]) -> Request:
    return req("sts.eu-west-1.amazonaws.com", "POST", "/", "", FORM, urllib.parse.urlencode({**params, "Version": "2011-06-15"}).encode())


def sm(op: str, body: dict[str, Any]) -> Request:
    return req(
        "secretsmanager.eu-west-1.amazonaws.com", "POST", "/", "", {"content-type": SM, "x-amz-target": f"secretsmanager.{op}"}, json.dumps(body).encode()
    )


def match(r: Request):
    o = ROUTES.match(r)
    assert o is not None, r.describe()
    return o


def model(name: str) -> dict[str, Any]:
    specs = os.environ.get("TOOLPASS_SPECS_DIR")
    path = Path(specs or "", f"{name}.spec")
    if not specs or not path.is_file():
        pytest.skip(f"no {name} description in TOOLPASS_SPECS_DIR")
    doc: dict[str, Any] = json.loads(path.read_bytes())
    return doc


# -- where requests go --------------------------------------------------------


def test_the_integration_exposes_the_table():
    assert INTEGRATION.routes() is ROUTES


def test_endpoints_cover_the_partition_and_its_global_hosts():
    hosts = {h for h, prefix in ROUTES.endpoints(Settings("aws-prod", "aws", {"partition": "aws", "region": "eu-west-1"})) if prefix == ""}
    assert {"s3.eu-west-1.amazonaws.com", "s3.amazonaws.com", "lambda.us-east-1.amazonaws.com", "secretsmanager.ap-south-1.amazonaws.com"} <= hosts
    assert {"iam.amazonaws.com", "sts.amazonaws.com", "sts.eu-west-1.amazonaws.com"} <= hosts
    assert not any(h.startswith(("ec2.", "rds.", "eks.", "ssm.")) or h.endswith(".cn") for h in hosts)
    cn = {h for h, _ in ROUTES.endpoints(Settings("aws-cn", "aws", {"partition": "aws-cn", "region": "cn-north-1"}))}
    assert "iam.cn-north-1.amazonaws.com.cn" in cn and "s3.cn-northwest-1.amazonaws.com.cn" in cn and "iam.amazonaws.com" not in cn
    # S3's dash-style regional, dualstack and Transfer Acceleration hosts are claimed too.
    assert {
        "s3-us-west-2.amazonaws.com",
        "s3.dualstack.eu-west-1.amazonaws.com",
        "s3-accelerate.amazonaws.com",
        "s3-accelerate.dualstack.amazonaws.com",
    } <= hosts
    assert not any(h.startswith("s3-cn-") for h in cn) and "s3-accelerate.amazonaws.com" not in cn
    gov = {h for h, _ in ROUTES.endpoints(Settings("aws-gov", "aws", {"partition": "aws-us-gov", "region": "us-gov-west-1"}))}
    assert "s3-us-gov-west-1.amazonaws.com" in gov and "s3-accelerate.amazonaws.com" not in gov
    # Every claimed host is one the table can place.
    assert all(place(h) is not None for h in hosts | cn | gov)


def test_dash_style_and_accelerate_hosts_route_path_style_requests():
    for host in ("s3-us-west-2.amazonaws.com", "s3-accelerate.amazonaws.com"):
        o = match(req(host, "GET", "/reports/2026/q3.csv"))
        assert (o.action, o.resource, o.unresolved) == ("raw:s3:GetObject", "arn:aws:s3:::reports/2026/q3.csv", None)


@pytest.mark.parametrize(
    ("host", "where"),
    [
        ("s3.eu-west-1.amazonaws.com", ("s3", "eu-west-1", "aws")),
        ("s3.dualstack.us-east-2.amazonaws.com", ("s3", "us-east-2", "aws")),
        ("lambda.us-gov-west-1.amazonaws.com", ("lambda", "us-gov-west-1", "aws-us-gov")),
        ("secretsmanager-fips.us-east-1.amazonaws.com", ("secretsmanager", "us-east-1", "aws")),
        ("iam.amazonaws.com", ("iam", "us-east-1", "aws")),
        ("STS.amazonaws.com", ("sts", "us-east-1", "aws")),
        ("s3.cn-north-1.amazonaws.com.cn", ("s3", "cn-north-1", "aws-cn")),
        ("s3.cn-north-1.amazonaws.com", None),
        ("ec2.eu-west-1.amazonaws.com", None),
        ("bucket.s3.eu-west-1.amazonaws.com", None),
        ("s3-us-west-2.amazonaws.com", ("s3", "us-west-2", "aws")),
        ("s3-us-gov-west-1.amazonaws.com", ("s3", "us-gov-west-1", "aws-us-gov")),
        ("s3-accelerate.amazonaws.com", ("s3", "us-east-1", "aws")),
        ("s3-accelerate.dualstack.amazonaws.com", ("s3", "us-east-1", "aws")),
        ("s3-cn-north-1.amazonaws.com", None),
        # A virtual-hosted bucket is never placed: its path is not the bucket's.
        ("bucket.s3.amazonaws.com", None),
        ("bucket.s3-accelerate.amazonaws.com", None),
    ],
)
def test_place(host, where):
    got = place(host)
    assert (tuple(got) if got is not None else None) == where


# -- the service models --------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "table", "unrouted"),
    [("aws-iam", IAM, IAM_UNROUTED), ("aws-sts", STS, STS_UNROUTED), ("aws-secretsmanager", SECRETSMANAGER, SECRETSMANAGER_UNROUTED)],
)
def test_every_rpc_operation_is_routed_or_left_out_with_a_reason(name, table, unrouted):
    ops = set(model(name)["operations"])
    assert not set(table) & set(unrouted)
    assert sorted(ops - set(table) - set(unrouted)) == []
    for op, spec in table.items():
        if spec.member and op in ops:
            members = model(name)["shapes"][model(name)["operations"][op]["input"]["shape"]]["members"]
            assert spec.member in members, (op, spec.member)


def _lambda_uri(op: dict[str, Any]) -> tuple[str, str, str]:
    path, _, query = op["http"]["requestUri"].partition("?")
    return op["http"]["method"], path.rstrip("/"), query


def test_every_lambda_operation_is_routed_or_left_out_with_a_reason():
    doc = model("aws-lambda")
    ops = doc["operations"]
    assert not set(LAMBDA) & set(LAMBDA_UNROUTED)
    assert sorted(set(ops) - set(LAMBDA) - set(LAMBDA_UNROUTED)) == []
    paths = {op: (m, p) for m, p, op in LAMBDA_PATHS}
    for name, op in ops.items():
        method, path, _ = _lambda_uri(op)
        if name in paths:
            assert paths[name] == (method, path), name


SAMPLES = {
    "FunctionName": urllib.parse.quote(FN, safe=""),
    "Resource": urllib.parse.quote(FN, safe=""),
    "CodeSigningConfigArn": urllib.parse.quote(f"arn:aws:lambda:eu-west-1:{ACCOUNT}:code-signing-config:csc-0123456789abcdef0", safe=""),
    "LayerName": urllib.parse.quote(f"arn:aws:lambda:eu-west-1:{ACCOUNT}:layer:libs", safe=""),
    "VersionNumber": "3",
    "DurableExecutionArn": urllib.parse.quote(f"{FN}:1/durable-execution/run/0123", safe=""),
    "Name": "live",
    "StatementId": "s1",
    "UUID": "14e0db71-0000-0000-0000-000000000000",
    "CapacityProviderName": "cp",
    "ResourceArn": urllib.parse.quote(FN, safe=""),
    "CallbackId": "cb",
}


def test_lambda_requests_are_recognised_as_the_operation_the_model_describes():
    doc = model("aws-lambda")
    for name, op in doc["operations"].items():
        method, path, literal = _lambda_uri(op)
        path = re.sub(r"\{(\w+)\}", lambda m: SAMPLES[m.group(1)], path)
        query = [literal] if literal else []
        members = doc["shapes"][op["input"]["shape"]]["members"] if "input" in op else {}
        required = doc["shapes"][op["input"]["shape"]].get("required", []) if "input" in op else []
        for member in required:
            loc = members[member].get("location")
            if loc == "querystring":
                value = f"arn:aws:lambda:eu-west-1:{ACCOUNT}:layer:libs:3" if member == "Arn" else "live"
                query.append(f"{members[member]['locationName']}={urllib.parse.quote(value, safe='')}")
        o = match(lam(method, path, "&".join(query)))
        assert o.fields.get("operation") == name, (name, o.unresolved)
        if name in LAMBDA and not LAMBDA[name].forbid:
            assert o.unresolved is None, (name, o.unresolved)


# S3 operations that send the same request as a newer one.
S3_SAME_AS = {
    "GetBucketLifecycle": "GetBucketLifecycleConfiguration",
    "PutBucketLifecycle": "PutBucketLifecycleConfiguration",
    "GetBucketNotification": "GetBucketNotificationConfiguration",
    "PutBucketNotification": "PutBucketNotificationConfiguration",
}
S3_OTHER_HOSTS = {"ListDirectoryBuckets", "WriteGetObjectResponse"}


def test_every_s3_operation_is_routed_or_left_out_and_recognised():
    doc = model("aws-s3")
    assert not set(S3) & set(S3_UNROUTED)
    for name, op in doc["operations"].items():
        canonical = S3_SAME_AS.get(name, name)
        assert canonical in S3 or canonical in S3_UNROUTED, name
        if name in S3_OTHER_HOSTS:
            continue
        path, _, literal = op["http"]["requestUri"].partition("?")
        path = path.replace("{Bucket}", "bucket-1").replace("{Key+}", "dir/key.txt")
        query = [literal] if literal else []
        headers: dict[str, str] = {}
        shape = doc["shapes"][op["input"]["shape"]] if "input" in op else {}
        for member in shape.get("required", []):
            m = shape["members"][member]
            if m.get("location") == "querystring":
                query.append(f"{m['locationName']}=1")
            elif m.get("location") == "header":
                headers[m["locationName"].lower()] = "/other-bucket/key"
        o = match(s3(op["http"]["method"], path, "&".join(query), headers))
        assert o.fields.get("operation") == canonical, (name, o.fields, o.unresolved)
        if canonical in S3:
            assert o.unresolved is None, (name, o.unresolved)
            action, _ = S3[canonical]
            assert o.action == f"raw:s3:{action}", name


# -- routing -------------------------------------------------------------------

BUCKET = "arn:aws:s3:::reports"
OBJECT = "arn:aws:s3:::reports/2026/q3.csv"


@pytest.mark.parametrize(
    ("r", "action", "resource", "effect"),
    [
        (s3("GET", "/"), "raw:s3:ListAllMyBuckets", "all", "read"),
        (s3("GET", "/reports", "list-type=2&prefix=2026%2F"), "raw:s3:ListBucket", BUCKET, "read"),
        (s3("HEAD", "/reports"), "raw:s3:ListBucket", BUCKET, "read"),
        (s3("GET", "/reports/2026/q3.csv"), "raw:s3:GetObject", OBJECT, "read"),
        (s3("GET", "/reports/2026%2Fq3.csv", "x-id=GetObject"), "raw:s3:GetObject", OBJECT, "read"),
        (s3("GET", "/reports/2026/q3.csv", "versionId=3HL4kqtJlcpXroDTDmJ"), "raw:s3:GetObjectVersion", OBJECT, "read"),
        (s3("HEAD", "/reports/2026/q3.csv"), "raw:s3:GetObject", OBJECT, "read"),
        (s3("PUT", "/reports/2026/q3.csv", "", {"content-type": "text/csv"}, b"a,b\n"), "raw:s3:PutObject", OBJECT, "write"),
        (s3("PUT", "/reports/2026/q3.csv", "partNumber=2&uploadId=abc"), "raw:s3:PutObject", OBJECT, "write"),
        (s3("POST", "/reports/2026/q3.csv", "uploads"), "raw:s3:PutObject", OBJECT, "write"),
        (s3("DELETE", "/reports/2026/q3.csv"), "raw:s3:DeleteObject", OBJECT, "destructive"),
        (s3("DELETE", "/reports/2026/q3.csv", "versionId=v1"), "raw:s3:DeleteObjectVersion", OBJECT, "destructive"),
        (s3("DELETE", "/reports/2026/q3.csv", "uploadId=abc"), "raw:s3:AbortMultipartUpload", OBJECT, "destructive"),
        (s3("PUT", "/reports/2026/q3.csv", "tagging"), "raw:s3:PutObjectTagging", OBJECT, "write"),
        (s3("POST", "/reports/2026/q3.csv", "select&select-type=2"), "raw:s3:GetObject", OBJECT, "read"),
        (s3("PUT", "/reports"), "raw:s3:CreateBucket", BUCKET, "write"),
        (s3("DELETE", "/reports"), "raw:s3:DeleteBucket", BUCKET, "destructive"),
        (s3("PUT", "/reports", "policy"), "raw:s3:PutBucketPolicy", BUCKET, "write"),
        (s3("PUT", "/reports", "acl", {"x-amz-acl": "private"}), "raw:s3:PutBucketAcl", BUCKET, "write"),
        (s3("PUT", "/reports/2026/q3.csv", "acl", {"x-amz-grant-read": "id=abc"}), "raw:s3:PutObjectAcl", OBJECT, "write"),
        (s3("DELETE", "/reports", "cors"), "raw:s3:PutBucketCORS", BUCKET, "destructive"),
        (s3("GET", "/reports", "inventory"), "raw:s3:GetInventoryConfiguration", BUCKET, "read"),
        (s3("DELETE", "/reports", "inventory&id=weekly"), "raw:s3:PutInventoryConfiguration", BUCKET, "destructive"),
        (req("s3.amazonaws.com", "GET", "/reports/2026/q3.csv"), "raw:s3:GetObject", OBJECT, "read"),
        (lam("POST", "/2015-03-31/functions/" + urllib.parse.quote(FN, safe="") + "/invocations", "", b"{}"), "raw:lambda:InvokeFunction", FN, "write"),
        (lam("POST", f"/2015-03-31/functions/{ACCOUNT}:function:report/invocations", "Qualifier=live"), "raw:lambda:InvokeFunction", FN + ":live", "write"),
        (lam("POST", f"/2015-03-31/functions/{FN}:7/invocations"), "raw:lambda:InvokeFunction", FN + ":7", "write"),
        (lam("GET", f"/2015-03-31/functions/{FN}"), "raw:lambda:GetFunction", FN, "read"),
        (lam("PUT", f"/2015-03-31/functions/{FN}/code", "", b'{"ZipFile": "UEsDBA=="}'), "raw:lambda:UpdateFunctionCode", FN, "write"),
        (lam("PUT", f"/2015-03-31/functions/{FN}/configuration", "", b'{"Timeout": 30}'), "raw:lambda:UpdateFunctionConfiguration", FN, "write"),
        (lam("DELETE", f"/2015-03-31/functions/{FN}"), "raw:lambda:DeleteFunction", FN, "destructive"),
        (lam("DELETE", f"/2015-03-31/functions/{FN}/aliases/live"), "raw:lambda:DeleteAlias", FN, "destructive"),
        (
            lam("PUT", f"/2019-09-30/functions/{FN}/provisioned-concurrency", "Qualifier=live"),
            "raw:lambda:PutProvisionedConcurrencyConfig",
            FN + ":live",
            "write",
        ),
        (lam("POST", f"/2017-03-31/tags/{FN}", "", b'{"Tags": {"team": "a"}}'), "raw:lambda:TagResource", FN, "write"),
        (lam("DELETE", f"/2017-03-31/tags/{FN}", "tagKeys=team"), "raw:lambda:UntagResource", FN, "write"),
        (lam("GET", "/2015-03-31/functions"), "raw:lambda:ListFunctions", "all", "read"),
        (
            lam("GET", "/2018-10-31/layers", f"find=LayerVersion&Arn=arn:aws:lambda:eu-west-1:{ACCOUNT}:layer:libs:3"),
            "raw:lambda:GetLayerVersion",
            f"arn:aws:lambda:eu-west-1:{ACCOUNT}:layer:libs:3",
            "read",
        ),
        (sm("GetSecretValue", {"SecretId": SECRET}), "raw:secretsmanager:GetSecretValue", SECRET, "read"),
        (sm("PutSecretValue", {"SecretId": SECRET, "SecretString": "x"}), "raw:secretsmanager:PutSecretValue", SECRET, "write"),
        (sm("DeleteSecret", {"SecretId": SECRET, "RecoveryWindowInDays": 7}), "raw:secretsmanager:DeleteSecret", SECRET, "destructive"),
        (sm("RotateSecret", {"SecretId": SECRET}), "raw:secretsmanager:RotateSecret", SECRET, "write"),
        (sm("ListSecrets", {}), "raw:secretsmanager:ListSecrets", "all", "read"),
        (
            sts({"Action": "AssumeRole", "RoleArn": f"arn:aws:iam::{ACCOUNT}:role/deploy", "RoleSessionName": "s"}),
            "raw:sts:AssumeRole",
            f"arn:aws:iam::{ACCOUNT}:role/deploy",
            "write",
        ),
        (
            req("sts.amazonaws.com", "GET", "/", f"Action=AssumeRole&Version=2011-06-15&RoleArn=arn:aws:iam::{ACCOUNT}:role/x&RoleSessionName=s"),
            "raw:sts:AssumeRole",
            f"arn:aws:iam::{ACCOUNT}:role/x",
            "write",
        ),
        (sts({"Action": "DecodeAuthorizationMessage", "EncodedMessage": "abc"}), "raw:sts:DecodeAuthorizationMessage", "all", "read"),
        (iam({"Action": "ListRoles"}), "raw:iam:ListRoles", "all", "read"),
        (
            iam({"Action": "CreatePolicyVersion", "PolicyArn": f"arn:aws:iam::{ACCOUNT}:policy/ops/deploy", "PolicyDocument": "{}"}),
            "raw:iam:CreatePolicyVersion",
            f"arn:aws:iam::{ACCOUNT}:policy/ops/deploy",
            "write",
        ),
        (
            iam({"Action": "DeletePolicy", "PolicyArn": f"arn:aws:iam::{ACCOUNT}:policy/old"}),
            "raw:iam:DeletePolicy",
            f"arn:aws:iam::{ACCOUNT}:policy/old",
            "destructive",
        ),
        (
            iam({"Action": "SimulatePrincipalPolicy", "PolicySourceArn": f"arn:aws:iam::{ACCOUNT}:role/x", "ActionNames.member.1": "s3:GetObject"}),
            "raw:iam:SimulatePrincipalPolicy",
            f"arn:aws:iam::{ACCOUNT}:role/x",
            "read",
        ),
        # A write sent as a GET is still a write.
        (
            req("iam.amazonaws.com", "GET", "/", f"Action=DeletePolicy&Version=2010-05-08&PolicyArn=arn:aws:iam::{ACCOUNT}:policy/old"),
            "raw:iam:DeletePolicy",
            f"arn:aws:iam::{ACCOUNT}:policy/old",
            "destructive",
        ),
    ],
)
def test_routing(r, action, resource, effect):
    o = match(r)
    assert o.unresolved is None, o.unresolved
    assert (o.action, o.resource, o.effect) == (action, resource, effect)
    # The action is one the aws integration checks, and the resource parses as it does.
    assert find_action(INTEGRATION, action) is not None
    if resource != "all":
        assert parse_resource(parse_ref(resource), "aws") == resource


def test_the_agents_own_identity_is_not_checked():
    o = match(sts({"Action": "GetCallerIdentity"}))
    assert (o.action, o.unresolved) == (None, None)


def test_s3_presigned_url_parameters_select_nothing():
    q = (
        "X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Credential=AKIA%2F20261016%2Feu-west-1%2Fs3%2Faws4_request&X-Amz-Date=20261016T000000Z"
        "&X-Amz-Expires=300&X-Amz-SignedHeaders=host&X-Amz-Signature=abc&X-Amz-Security-Token=tok&x-id=GetObject"
    )
    o = match(s3("GET", "/reports/2026/q3.csv", q))
    assert (o.action, o.resource, o.unresolved) == ("raw:s3:GetObject", OBJECT, None)
    # Lowercase spellings are the same parameters.
    assert match(s3("GET", "/reports/2026/q3.csv", q.lower())).unresolved is None
    # An ACL set through the query is what PutObjectAcl sets, as it is through the header.
    o = match(s3("PUT", "/reports/2026/q3.csv", "acl&x-amz-acl=private"))
    assert (o.action, o.unresolved) == ("raw:s3:PutObjectAcl", None)


def test_s3_writes_and_lambda_invocations_send_data_out():
    assert match(s3("PUT", "/reports/k")).sends_out
    assert not match(s3("GET", "/reports/k")).sends_out
    o = match(lam("POST", f"/2015-03-31/functions/{FN}/invocations"))
    assert o.sends_out and o.untrusted_output
    assert not match(sm("PutSecretValue", {"SecretId": SECRET, "SecretString": "x"})).sends_out


# -- refusals ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("r", "why"),
    [
        # S3: a second resource, a second action, an ambiguous request, a name that cannot be checked exactly.
        (s3("PUT", "/reports/copy.csv", "", {"x-amz-copy-source": "/secret/data.csv"}), "copy"),
        (s3("PUT", "/reports/k", "partNumber=1&uploadId=u", {"x-amz-copy-source": "/secret/data.csv"}), "copy"),
        (s3("POST", "/reports", "delete", {"content-type": "application/xml"}, b"<Delete/>"), "every object"),
        (s3("PUT", "/reports/k", "", {"x-amz-acl": "public-read"}), "ACL"),
        (s3("PUT", "/reports/k", "", {"x-amz-grant-read": "uri=http://acs.amazonaws.com/groups/global/AllUsers"}), "grant"),
        (s3("PUT", "/reports/k", "", {"x-amz-tagging": "a=b"}), "tags"),
        (s3("PUT", "/reports/k", "", {"x-amz-object-lock-mode": "GOVERNANCE"}), "object lock"),
        (s3("PUT", "/reports/k", "", {"x-amz-server-side-encryption-aws-kms-key-id": "arn:aws:kms:eu-west-1:1:key/x"}), "KMS"),
        (s3("DELETE", "/reports/k", "", {"x-amz-bypass-governance-retention": "true"}), "governance"),
        (s3("PUT", "/reports", "", {"x-amz-acl": "private"}), "ACL"),
        (s3("PUT", "/reports", "replication"), "role"),
        (s3("GET", "/reports", "acl&policy"), "several"),
        (s3("GET", "/reports/k", "versionId=a&versionId=b"), "more than once"),
        (s3("GET", "/reports", "newSubresource"), "not one the table knows"),
        (s3("GET", "/Reports/k"), "bucket name"),
        (s3("GET", "/reports/a/../b"), "segment"),
        (s3("GET", "/reports/a%0Ab"), "control"),
        (s3("GET", "/reports/a%3Fb"), "?"),
        (s3("POST", "/reports"), "not an S3 operation"),
        (s3("POST", "/reports/k", "restore", {}, b"<RestoreRequest><OutputLocation/></RestoreRequest>"), "second bucket"),
        (s3("PUT", "/reports/k", "", {"x-http-method-override": "DELETE"}), "override"),
        # S3 reads an x-amz-* query parameter as the header of that name.
        (s3("PUT", "/reports/k", "x-amz-copy-source=secret%2Fdata.csv"), "copy"),
        (s3("PUT", "/reports/k", "partNumber=1&uploadId=u&X-Amz-Copy-Source=secret%2Fdata.csv"), "copy"),
        (s3("PUT", "/reports/k", "x-amz-acl=public-read"), "ACL"),
        (s3("PUT", "/reports/k", "X-Amz-Tagging=a%3Db"), "tags"),
        (s3("PUT", "/reports/k", "x-amz-grant-read=uri%3Dhttp%3A%2F%2Facs.amazonaws.com%2Fgroups%2Fglobal%2FAllUsers"), "grant"),
        (s3("PUT", "/reports/k", "X-Amz-Object-Lock-Mode=GOVERNANCE"), "object lock"),
        (s3("PUT", "/reports/k", "x-amz-server-side-encryption-aws-kms-key-id=arn%3Aaws%3Akms%3Aeu-west-1%3A1%3Akey%2Fx"), "KMS"),
        (s3("PUT", "/reports", "x-amz-bucket-object-lock-enabled=true"), "object lock"),
        # Lambda: the function by name, a second target, ambiguity.
        (lam("POST", "/2015-03-31/functions/report/invocations"), "by ARN"),
        (lam("POST", f"/2015-03-31/functions/{FN}:live/invocations", "Qualifier=prod"), "both"),
        (lam("POST", f"/2015-03-31/functions/{FN.replace('eu-west-1', 'us-east-1')}/invocations"), "not a function ARN"),
        (lam("PUT", f"/2015-03-31/functions/{FN}/configuration", "", b'{"Role": "arn:aws:iam::123456789012:role/admin"}'), "PassRole"),
        (lam("PUT", f"/2015-03-31/functions/{FN}/configuration", "", b'{"Layers": ["arn:aws:lambda:eu-west-1:1:layer:x:1"]}'), "layer"),
        (lam("PUT", f"/2015-03-31/functions/{FN}/code", "", b'{"S3Bucket": "other", "S3Key": "code.zip"}'), "S3"),
        (lam("PUT", f"/2015-03-31/functions/{FN}/configuration", "", b"not json"), "not JSON"),
        (lam("POST", "/2015-03-31/functions", "", b'{"FunctionName": "x", "Role": "r"}'), "execution role"),
        (lam("PUT", "/2015-03-31/event-source-mappings/14e0db71-0000-0000-0000-000000000000"), "mapping"),
        (lam("GET", f"/2019-09-30/functions/{FN}/provisioned-concurrency"), "Qualifier"),
        (lam("GET", "/2099-01-01/functions"), "not a Lambda operation"),
        # Secrets Manager: a secret by name or partial ARN, a second target, ambiguity.
        (sm("GetSecretValue", {"SecretId": "prod/db"}), "full ARN"),
        (sm("GetSecretValue", {"SecretId": SECRET[:-7]}), "full ARN"),
        (sm("GetSecretValue", {"SecretId": SECRET.replace("eu-west-1", "us-east-1")}), "full ARN"),
        # A name that itself ends in a hyphen and six characters: the ARN could be a partial ARN of another secret.
        (sm("GetSecretValue", {"SecretId": f"arn:aws:secretsmanager:eu-west-1:{ACCOUNT}:secret:prod-abcdef-AbCdEf"}), "partial ARN"),
        (sm("DeleteSecret", {"SecretId": f"arn:aws:secretsmanager:eu-west-1:{ACCOUNT}:secret:team/db-123456-x1Y2z3"}), "partial ARN"),
        (sm("UpdateSecret", {"SecretId": SECRET, "KmsKeyId": "alias/other"}), "KMS"),
        (sm("RotateSecret", {"SecretId": SECRET, "RotationLambdaARN": FN}), "rotation function"),
        (sm("ReplicateSecretToRegions", {"SecretId": SECRET, "AddReplicaRegions": [{"Region": "us-east-1", "KmsKeyId": "k"}]}), "KMS"),
        (sm("BatchGetSecretValue", {"SecretIdList": [SECRET]}), "several secrets"),
        (sm("CreateSecret", {"Name": "x"}), "not routed"),
        (
            req(
                "secretsmanager.eu-west-1.amazonaws.com",
                "POST",
                "/",
                "",
                {"content-type": SM, "x-amz-target": "secretsmanager.GetSecretValue"},
                b'{"SecretId": "%s", "secretid": "other"}' % SECRET.encode(),
            ),
            "more than once",
        ),
        (
            req(
                "secretsmanager.eu-west-1.amazonaws.com",
                "POST",
                "/",
                "",
                {"content-type": "application/json", "x-amz-target": "secretsmanager.GetSecretValue"},
                b"{}",
            ),
            "x-amz-json-1.1",
        ),
        (req("secretsmanager.eu-west-1.amazonaws.com", "POST", "/", "", {"content-type": SM, "x-amz-target": "kms.Decrypt"}, b"{}"), "X-Amz-Target"),
        # STS and IAM: names instead of ARNs, extra actions, ambiguous parameters.
        (
            sts(
                {
                    "Action": "AssumeRole",
                    "RoleArn": f"arn:aws:iam::{ACCOUNT}:role/deploy",
                    "RoleSessionName": "s",
                    "Tags.member.1.Key": "k",
                    "Tags.member.1.Value": "v",
                }
            ),
            "TagSession",
        ),
        (sts({"Action": "AssumeRole", "RoleArn": f"arn:aws:iam::{ACCOUNT}:role/deploy", "RoleSessionName": "s", "SourceIdentity": "me"}), "SetSourceIdentity"),
        (sts({"Action": "AssumeRole", "RoleArn": f"arn:aws:iam::{ACCOUNT}:user/deploy", "RoleSessionName": "s"}), "ARN of the role"),
        (sts({"Action": "AssumeRole", "RoleArn": "arn:aws-cn:iam::123456789012:role/deploy", "RoleSessionName": "s"}), "ARN of the role"),
        (sts({"Action": "AssumeRoleWithWebIdentity", "RoleArn": f"arn:aws:iam::{ACCOUNT}:role/x"}), "trust policy"),
        (sts({"Action": "GetSessionToken"}), "temporary credentials"),
        (sts({"Action": "GetSessionToken", "DurationSeconds": "3600", "SerialNumber": f"arn:aws:iam::{ACCOUNT}:mfa/me", "TokenCode": "123456"}), "not routed"),
        (sts({"Action": "GetFederationToken", "Name": "x"}), "not routed"),
        (iam({"Action": "AttachRolePolicy", "RoleName": "deploy", "PolicyArn": "arn:aws:iam::aws:policy/AdministratorAccess"}), "by name"),
        (iam({"Action": "CreateUser", "UserName": "mallory"}), "account id"),
        (iam({"Action": "GetPolicy", "PolicyArn": "arn:aws:iam::aws:policy/ReadOnlyAccess"}), "ARN of the policy"),
        (iam({"Action": "NoSuchOperation"}), "not routed"),
        (req("iam.amazonaws.com", "POST", "/", "Action=ListRoles", FORM, b"Action=DeleteRole&RoleName=x"), "more than once"),
        (req("iam.amazonaws.com", "POST", "/", "", FORM, b"Action=ListRoles&action=DeleteRole"), "more than once"),
        (req("iam.amazonaws.com", "POST", "/", "", {"content-type": "application/json"}, b'{"Action": "ListRoles"}'), "form-urlencoded"),
        (req("iam.amazonaws.com", "POST", "/", "", FORM, b""), "no Action"),
    ],
)
def test_refusals(r, why):
    o = match(r)
    assert o.unresolved is not None and why in o.unresolved, o.unresolved


def test_every_route_resolves_to_a_known_action_or_refuses():
    # The catch-alls' own action is a placeholder that fails the check if it ever reached one.
    assert all(r.action == "raw" for r in ROUTES.routes)
    assert find_action(INTEGRATION, "raw") is None


def test_rpc_tables_name_known_actions():
    for service, table in (("iam", IAM), ("sts", STS), ("secretsmanager", SECRETSMANAGER)):
        for op in table:
            assert find_action(INTEGRATION, f"raw:{service}:{op}") is not None
    for op, spec in LAMBDA.items():
        assert find_action(INTEGRATION, f"raw:lambda:{spec.action or op}") is not None
    for action, effect in S3.values():
        assert find_action(INTEGRATION, f"raw:s3:{action}") is not None and effect in ("read", "write", "destructive")

"""How EKS names an IAM principal inside a cluster: access entries
(eks:DescribeAccessEntry) and the older aws-auth ConfigMap in kube-system.

Both map an IAM role or user ARN to a Kubernetes username and Kubernetes
groups. The functions here are pure; the aws connection calls EKS and the
kubernetes connection reads the ConfigMap.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

import yaml

__all__ = [
    "EKS_CLUSTER_RE",
    "EKSMapping",
    "arn_candidates",
    "match_aws_auth",
    "merge_mappings",
    "parse_aws_auth",
    "render_username",
    "validate_eks_cluster",
]

# EKS cluster names: 1-100 characters, a letter or digit first, then
# letters, digits, hyphens and underscores.
EKS_CLUSTER_RE = re.compile(r"[0-9A-Za-z][A-Za-z0-9_-]{0,99}")

# arn:<partition>:iam::<account>:role/<path/>name (or user/...).
_IAM_ARN_RE = re.compile(r"(arn:aws[a-z-]*:iam::[0-9]{12}:(?:role|user))/(?:[^/]+/)*([^/]+)")

_PLACEHOLDER_RE = re.compile(r"\{\{([A-Za-z0-9]+)\}\}")


def validate_eks_cluster(v: str) -> None:
    if not EKS_CLUSTER_RE.fullmatch(v):
        raise ValueError(f"eks_cluster {v!r} must be an EKS cluster name: up to 100 letters, digits, hyphens and underscores")


@dataclass(frozen=True)
class EKSMapping:
    """One access entry or aws-auth row that matched a principal."""

    principal: str
    username: str
    groups: tuple[str, ...]


def arn_candidates(arn: str) -> list[str]:
    """The ARN as is, then with its IAM path removed when it has one.

    UNVERIFIED: AWSReservedSSO roles live under
    /aws-reserved/sso.amazonaws.com/[<region>/], and EKS (access entries and
    aws-auth alike) is understood to store and match role ARNs without the
    path, so arn:aws:iam::1:role/aws-reserved/sso.amazonaws.com/AWSReservedSSO_X_0
    is looked up as arn:aws:iam::1:role/AWSReservedSSO_X_0 as well. For
    aws-auth the AWS documentation states the rolearn may not include a path."""
    out = [arn]
    m = _IAM_ARN_RE.fullmatch(arn)
    if m is not None:
        short = m.group(1) + "/" + m.group(2)
        if short != arn:
            out.append(short)
    return out


def _account(arn: str) -> str:
    parts = arn.split(":")
    return parts[4] if len(parts) > 5 else ""


def render_username(username: str, principal: str, email: str) -> str:
    """The Kubernetes username of a mapping, with EKS's placeholders filled
    the way EKS fills them for a session of the principal. "" when the
    username uses a placeholder toolpass cannot know (the caller's
    username_template then applies).

    UNVERIFIED: {{SessionName}} is the role session name with "@" replaced
    by "-" and {{SessionNameRaw}} the session name as is; for an IAM
    Identity Center role the session name is taken to be the user's email.
    """
    if username == "":
        return ""
    unknown = False

    def fill(m: re.Match[str]) -> str:
        nonlocal unknown
        name = m.group(1)
        if name == "AccountID":
            return _account(principal)
        if name == "SessionNameRaw":
            return email
        if name == "SessionName":
            return email.replace("@", "-")
        unknown = True
        return ""

    out = _PLACEHOLDER_RE.sub(fill, username)
    if unknown or "{{" in out:
        return ""
    return out


def merge_mappings(found: Iterable[EKSMapping], email: str) -> tuple[str, tuple[str, ...]]:
    """The username (only when exactly one mapping matched) and the union of
    the groups, in order of first appearance."""
    rows = list(found)
    groups: list[str] = []
    for m in rows:
        for g in m.groups:
            if g != "" and g not in groups:
                groups.append(g)
    username = render_username(rows[0].username, rows[0].principal, email) if len(rows) == 1 else ""
    return username, tuple(groups)


def _strs(v: Any) -> tuple[str, ...]:
    if not isinstance(v, list):
        return ()
    return tuple(x for x in v if isinstance(x, str))


def parse_aws_auth(data: Mapping[str, Any]) -> list[tuple[str, str, tuple[str, ...]]]:
    """(arn, username, groups) of every mapRoles and mapUsers row of the
    aws-auth ConfigMap's data. Malformed rows are skipped; a value that is
    not YAML raises ValueError."""
    out: list[tuple[str, str, tuple[str, ...]]] = []
    for key, arn_key in (("mapRoles", "rolearn"), ("mapUsers", "userarn")):
        raw = data.get(key)
        if not isinstance(raw, str) or raw.strip() == "":
            continue
        try:
            rows = yaml.safe_load(raw)
        except yaml.YAMLError as e:
            raise ValueError(f"aws-auth {key} is not YAML: {type(e).__name__}") from None
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            arn = row.get(arn_key)
            if not isinstance(arn, str) or arn == "":
                continue
            username = row.get("username")
            out.append((arn, username if isinstance(username, str) else "", _strs(row.get("groups"))))
    return out


def match_aws_auth(data: Mapping[str, Any], principals: Iterable[str]) -> list[EKSMapping]:
    """The aws-auth rows for the principals: each principal ARN is tried as
    is and without its IAM path; the first row for an ARN wins."""
    rows = parse_aws_auth(data)
    found: list[EKSMapping] = []
    for p in principals:
        for cand in arn_candidates(p):
            row = next((r for r in rows if r[0] == cand), None)
            if row is not None:
                found.append(EKSMapping(principal=p, username=row[1], groups=row[2]))
                break
    return found

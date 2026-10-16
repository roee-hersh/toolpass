"""The company the demo runs in: two people, five systems, and what each
person may do in each system. Every fake answers from this file, so the
permission checks toolpass makes against them reach the same answers a real
GitHub, Jira, Slack, Linear and Kubernetes would for these grants.

    dana@acme.dev  a developer on the shop team: writes code, ships to
                   staging, files bugs.
    sam@acme.dev   joined support this week: can read everything the shop
                   team does, and change nothing.
"""

from __future__ import annotations

DANA = "dana@acme.dev"
SAM = "sam@acme.dev"
ADMIN = "admin@acme.dev"  # the account toolpass itself uses to look permissions up
PEOPLE = {DANA: "Dana Levi", SAM: "Sam Cohen", ADMIN: "Toolpass Admin"}

# GitHub: organization acme; logins; repository permissions (read, write, admin).
GITHUB_ORG = "acme"
GITHUB_LOGINS = {DANA: "dana-levi", SAM: "sam-cohen", ADMIN: "acme-admin"}
GITHUB_REPOS = {"acme/shop": {DANA: "write", SAM: "read"}}

# Jira: project SHOP; issues; project permissions per person.
JIRA_PROJECT = "SHOP"
JIRA_ISSUES = {"SHOP-12": "Checkout button does nothing on Safari", "SHOP-14": "Add Apple Pay"}
JIRA_PERMISSIONS = {
    DANA: {"BROWSE_PROJECTS", "CREATE_ISSUES", "EDIT_ISSUES", "ADD_COMMENTS", "TRANSITION_ISSUES", "ASSIGN_ISSUES"},
    SAM: {"BROWSE_PROJECTS"},
}

# Slack: channels and who is in them. #releases is posting-restricted to its members who are not guests.
SLACK_CHANNELS = {
    "shop-dev": {"id": "C0SHOPDEV", "members": {DANA, SAM}, "private": False},
    "releases": {"id": "C0RELEASE", "members": {DANA}, "private": True},
}

# Linear: team ENG and its members.
LINEAR_TEAMS = {"ENG": {"id": "team-eng", "name": "Engineering", "members": {DANA}}}

# Kubernetes: what each person may do per namespace (Kubernetes RBAC verbs on resources).
K8S_NAMESPACES = ("shop-staging", "shop-prod")
K8S_DEPLOYMENTS = {("shop-staging", "shop-web"): "ghcr.io/acme/shop-web:1.4.0", ("shop-prod", "shop-web"): "ghcr.io/acme/shop-web:1.3.2"}
K8S_RBAC = {
    DANA: {"shop-staging": {"get", "list", "watch", "update", "patch"}, "shop-prod": {"get", "list", "watch"}},
    SAM: {"shop-staging": {"get", "list", "watch"}, "shop-prod": {"get", "list", "watch"}},
}

"""Okta Management API requests, mapped to the okta integration's actions.

The integration answers one question, ``group.member``: reading a group
(its profile, its members, its apps) is checked as membership of that
group, by id. Every write (users, groups and their memberships, apps,
policies) is administration no action covers, and is refused. Other reads
need no check: they return what the agent's own token sees, untrusted.
"""

from __future__ import annotations

from toolpass.core.routes import Route, RouteTable

__all__ = ["ROUTES"]

_G = "/api/v1/groups/{groupId}"
_GROUP = "group:{groupId}"

_ADMIN = "Okta administration: no okta action covers it"

ROUTES = RouteTable(
    "okta",
    (
        Route("GET", "/api/v1/users/me", None, "", description="the agent's own user"),
        Route("GET", _G, "group.member", _GROUP),
        Route("GET", _G + "/users", "group.member", _GROUP),
        Route("GET", _G + "/apps", "group.member", _GROUP),
    ),
    claims=("/api/v1",),
    unrouted={("*", "/api/v1/**"): _ADMIN},
)

"""Paths a server reads differently from their raw spelling are never routed:
a percent-encoded API prefix, dot segments, a Host header naming another host."""

from __future__ import annotations

import pytest

from toolpass.core.routes import Request
from toolpass.integrations.github.routes import ROUTES as GITHUB
from toolpass.integrations.slack.routes import ROUTES as SLACK


def req(method: str, path: str, host: str = "ghe.example") -> Request:
    return Request("https", host, 443, method, path, "", {"content-type": "application/json"}, b"{}")


@pytest.mark.parametrize(
    "path",
    [
        "/%61pi/v3/repos/acme/x/pulls",  # /api/v3 with an encoded letter
        "/api%2Fv3/repos/acme/x/pulls",  # the prefix's slash encoded
        "/api/v3/repos/acme/x/pulls/1/../../..",
        "/api/v3/repos/acme/x/issues/%2e%2e",
        "/api/v3/repos/acme/x/./pulls",
    ],
)
def test_encoded_prefixes_and_dot_segments_are_unresolved(path: str) -> None:
    for method in ("GET", "POST"):
        o = GITHUB.match(req(method, path), "/api/v3")
        assert o is not None and o.unresolved, (method, path, o)


def test_a_table_prefix_spelled_encoded_is_unresolved() -> None:
    o = SLACK.match(req("POST", "/%61pi/chat.postMessage", "slack.com"))
    assert o is not None and o.unresolved


def test_plain_paths_still_route() -> None:
    o = GITHUB.match(req("POST", "/api/v3/repos/acme/x/pulls"), "/api/v3")
    assert o is not None and o.unresolved is None and o.action == "pr.create"

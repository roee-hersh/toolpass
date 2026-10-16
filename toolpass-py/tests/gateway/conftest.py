"""No test reaches a real MCP server: the probe `gateway on` makes of an HTTP
server answers as Linear's would, asking for OAuth sign-in."""

from __future__ import annotations

import pytest

from toolpass import _mcp_http


@pytest.fixture(autouse=True)
def _no_network_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_mcp_http, "probe", lambda url, headers: "sign-in")

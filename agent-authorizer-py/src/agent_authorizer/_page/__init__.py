"""The dashboard page: one HTML file, its stylesheet and its script, read
once. Served by ``agent-authorizer serve`` at ``/dashboard/`` and by ``agent_authorizer.watch()``."""

from __future__ import annotations

import pathlib

__all__ = ["PAGE", "SCRIPT", "STYLE"]


def _asset(name: str) -> bytes:
    return (pathlib.Path(__file__).parent / name).read_bytes()


PAGE = _asset("index.html")
STYLE = _asset("watch.css")
SCRIPT = _asset("watch.js")

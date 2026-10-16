"""examples/demo/run.py: two people use the shop MCP server, built with
toolpass; a toolpass server decides every request each tool makes to GitHub,
Jira, Slack, Linear and Kubernetes, by the real integrations, and collects
every run for its dashboard."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Any

DEMO = Path(__file__).resolve().parents[2] / "examples" / "demo"
if str(DEMO) not in sys.path:
    sys.path.insert(0, str(DEMO))

import run  # noqa: E402
from shop_server import Systems  # noqa: E402


def _runs(collector: run.Collector, want: int) -> list[dict[str, Any]]:
    for _ in range(100):  # each shop server sends its runs as it exits; allow a slow runner a moment
        rows = collector.runs()
        if len(rows) >= want:
            return rows
        time.sleep(0.1)
    return collector.runs()


def _actions(run_: dict[str, Any]) -> list[tuple[str, str]]:
    return [(a.get("action") or "", a["outcome"]) for a in run_.get("actions", [])]


def test_the_tour() -> None:
    systems = Systems()
    collector = run.Collector(systems.connections)
    try:
        env = {**os.environ, **collector.env(), "DEMO_SYSTEMS": systems.shared()}
        env.pop("TOOLPASS_DASHBOARD", None)
        outcomes = run.tour(env, say=lambda _: None)
        assert [(u.split("@")[0], t, ok) for u, t, ok in outcomes] == [
            ("dana", "status", True),
            ("dana", "open_pr", True),
            ("dana", "ship", True),
            ("dana", "ship", False),  # prod
            ("dana", "report_bug", True),
            ("sam", "status", True),
            ("sam", "open_pr", False),
            ("sam", "ship", False),
            ("sam", "report_bug", False),
        ]
        # A refused request never reached the system: one pull request (Dana's), one patch (staging).
        assert systems.fakes["github"].seen.count("POST /api/v3/repos/acme/shop/pulls") == 1
        patches = [s for s in systems.fakes["kubernetes"].seen if s.startswith("PATCH")]
        assert patches == ["PATCH /apis/apps/v1/namespaces/shop-staging/deployments/shop-web"]
        runs = _runs(collector, len(run.TOUR))
    finally:
        collector.close()
        systems.close()

    # Each shop server reports its own runs, in the order it ran them.
    by_user = {u: [r for r in runs if r.get("user") == u] for u in (run.world.DANA, run.world.SAM)}
    assert [r["tool"] for r in by_user[run.world.DANA]] == [t for u, t, _ in run.TOUR if u == run.world.DANA]
    assert [r["tool"] for r in by_user[run.world.SAM]] == [t for u, t, _ in run.TOUR if u == run.world.SAM]
    assert {r["server"] for r in runs} == {"shop"}
    dana_pr, dana_prod = by_user[run.world.DANA][1], by_user[run.world.DANA][3]
    sam_pr, sam_bug = by_user[run.world.SAM][1], by_user[run.world.SAM][3]
    assert _actions(dana_pr) == [
        ("github:repo.read", "allowed"),
        ("github:pr.create", "allowed"),
        ("jira:ADD_COMMENTS", "allowed"),
        ("jira:BROWSE_PROJECTS", "allowed"),
        ("jira:TRANSITION_ISSUES", "allowed"),
        ("slack:message.post", "allowed"),
    ]
    assert _actions(dana_prod) == [("kubernetes:raw:get:deployments.apps", "allowed"), ("kubernetes:raw:patch:deployments.apps", "refused")]
    assert _actions(sam_pr) == [("github:repo.read", "allowed"), ("github:pr.create", "refused")]
    assert _actions(sam_bug) == [("linear:team.member", "refused")]
    assert "not a member of team ENG" in sam_bug["reason"]

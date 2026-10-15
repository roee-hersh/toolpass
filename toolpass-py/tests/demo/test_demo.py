"""examples/demo/run.py: two people use the shop MCP server through the
gateway; every call, and every request each tool made to GitHub, Jira, Slack,
Linear and Kubernetes, lands in the record, decided by the real integrations."""

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

from toolpass._store import Store  # noqa: E402


def _calls(data: Path, want: int) -> list[dict[str, Any]]:
    store = Store(data / "gateway.db")
    for _ in range(100):  # each server's writer drains at its exit; allow a slow runner a moment
        rows, _ = store.calls(0)
        if len(rows) == want and all(r["inside"] for r in rows):
            return rows
        time.sleep(0.1)
    return store.calls(0)[0]


def _actions(call: dict[str, Any]) -> list[tuple[str, str]]:
    [event] = call["inside"]
    return [(a.get("action") or "", a["outcome"]) for a in event["actions"]]


def test_the_tour(tmp_path: Path) -> None:
    systems = Systems()
    try:
        env = {**os.environ, "TOOLPASS_DATA_DIR": str(tmp_path), "DEMO_SYSTEMS": systems.shared()}
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
    finally:
        systems.close()

    calls = sorted(_calls(tmp_path, len(run.TOUR)), key=lambda c: c["started"])  # in the order the tour sent them
    assert [c["tool"] for c in calls] == [t for _, t, _ in run.TOUR]
    # One tool run under each call; on failure, where each call's runs went.
    assert [len(c["inside"]) for c in calls] == [1] * len(calls), [
        (c["id"], c["tool"], c["started"], c["ended"], [e.get("tool") for e in c["inside"]]) for c in calls
    ]
    dana_pr, dana_prod, sam_pr, sam_bug = calls[1], calls[3], calls[6], calls[8]
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
    assert "not a member of team ENG" in sam_bug["inside"][0]["reason"]

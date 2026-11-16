"""examples/wire_agent.py runs and refuses what it should."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def test_wire_example_runs():
    path = Path(__file__).resolve().parents[2] / "examples" / "wire_agent.py"
    spec = importlib.util.spec_from_file_location("wire_agent", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["wire_agent"] = module  # the example's dataclasses resolve their annotations through sys.modules
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop("wire_agent", None)
    outcomes = module.main(say=lambda _: None)
    assert outcomes == [
        ("propose_replicas(acme/gitops, 3) as dana", "not_authorized"),
        ("propose_replicas(acme/gitops, 3) as admin", "ran"),
    ]
    # Dana's read happened (as the agent); her pull request never reached GitHub.
    assert module.FakeGitHub.seen == [
        "GET /api/v3/repos/acme/gitops/contents/deploy.yaml",
        "GET /api/v3/repos/acme/gitops/contents/deploy.yaml",
        "POST /api/v3/repos/acme/gitops/pulls",
    ]

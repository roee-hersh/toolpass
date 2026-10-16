"""`toolpass routes`: the route tables, listed."""

from __future__ import annotations

import io

from toolpass import cli


def capture(*args: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = cli.run(list(args), out, err)
    return code, out.getvalue(), err.getvalue()


def test_routes_lists_every_integration():
    code, out, err = capture("routes")
    assert code == 0 and err == ""
    lines = dict(line.split(None, 1) for line in out.splitlines())
    assert lines["github"].startswith(("2", "3"))  # hundreds of routes
    assert lines["zendesk"] == "no route(s)"


def test_routes_for_one_integration():
    code, out, err = capture("routes", "github")
    assert code == 0 and err == ""
    assert "POST    /repos/{owner}/{repo}/pulls" in out
    assert "pr.create" in out and "repo:{owner}/{repo}" in out
    code, out, err = capture("routes", "slack")
    assert code == 0
    assert "/chat.postMessage" in out and "sends out" in out and "left out on purpose" in out


def test_routes_unknown_or_undescribed_integration():
    code, _, err = capture("routes", "nope")
    assert code == 1 and "unknown integration" in err
    code, _, err = capture("routes", "zendesk")
    assert code == 1 and "not described yet" in err


def test_usage_mentions_routes():
    _, out, _ = capture("help")
    assert "toolpass routes" in out

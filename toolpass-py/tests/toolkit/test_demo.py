"""``toolpass demo`` runs and every step turns out as the README says."""

from __future__ import annotations

import io

from toolpass import _demo, cli


def test_demo_outcomes() -> None:
    lines: list[str] = []
    outcomes = _demo.run(lines.append, auto=True)
    assert outcomes == [
        ("get_issue(PAY-123)", "ran"),
        ("delete_issue(PAY-123)", "not_authorized"),  # dana may not: the engine said so
        ("delete_issue(PAY-123)", "approval_pending"),  # admin may; a person confirms
        ("delete_issue(PAY-123) retried, after a person decided", "ran"),  # approved, runs once
        ("delete_issue(OPS-9), another project", "out_of_scope"),
    ]
    text = "\n".join(lines)
    assert "watch it live: http://127.0.0.1:" in text
    assert "dana@example.com is not an admin" in text


def test_demo_command_auto() -> None:
    out, err = io.StringIO(), io.StringIO()
    assert cli.run(["demo", "-auto"], out, err) == 0
    assert err.getvalue() == ""
    assert "[refused] delete_issue(PAY-123)" in out.getvalue() and "[ran]     delete_issue(PAY-123) retried," in out.getvalue()


def test_demo_command_rejects_arguments_and_a_bad_listen_address() -> None:
    out, err = io.StringIO(), io.StringIO()
    assert cli.run(["demo", "extra"], out, err) == 2 and "unexpected argument" in err.getvalue()
    out, err = io.StringIO(), io.StringIO()
    assert cli.run(["demo", "-auto", "-listen", "0.0.0.0:0"], out, err) == 2 and "loopback" in err.getvalue()


def test_demo_times_out_without_a_person() -> None:
    lines: list[str] = []
    outcomes = _demo.run(lines.append, timeout=0.5)
    assert ("delete_issue(PAY-123) retried", "timeout") in outcomes
    assert any(line.startswith("[timeout]") for line in lines)

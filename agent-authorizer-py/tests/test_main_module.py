"""``python -m agent_authorizer`` runs the command line, as the ``agent-authorizer`` script does."""

from __future__ import annotations

import subprocess
import sys


def test_python_m_agent_authorizer_runs_the_cli() -> None:
    out = subprocess.run([sys.executable, "-m", "agent_authorizer", "help"], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert "gateway" in out.stdout

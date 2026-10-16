"""``python -m toolpass`` runs the command line, as the ``toolpass`` script does."""

from __future__ import annotations

import subprocess
import sys


def test_python_m_toolpass_runs_the_cli() -> None:
    out = subprocess.run([sys.executable, "-m", "toolpass", "help"], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert "gateway" in out.stdout

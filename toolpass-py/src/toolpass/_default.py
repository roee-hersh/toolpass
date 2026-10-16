"""The module-level ``tool``, on a default toolkit set up by ``configure``.

    import toolpass

    toolpass.configure(credentials={"github-bot": token})

    @toolpass.tool(effect="write", scope={"repo": "acme/gitops-*"}, credential="github-bot")
    def open_pr(repo: str, title: str, *, credential: str) -> str: ...

Tools declared before ``configure`` use its settings too: they read them at
call time. A program that needs two sets of settings makes its own
``Toolkit`` objects and uses their ``tool``.
"""

from __future__ import annotations

from typing import Any

from toolpass._toolkit import Toolkit

_DEFAULT = Toolkit()
_DEFAULT._lenient = True


def configure(**settings: Any) -> Toolkit:
    """Set up the default toolkit behind ``tool``, with the same
    settings as ``Toolkit(...)``. Settings left out return to their defaults.
    Returns the default toolkit."""
    _DEFAULT._configure(**settings)
    return _DEFAULT


def default_toolkit() -> Toolkit:
    """The toolkit behind the module-level ``tool``."""
    return _DEFAULT


tool = _DEFAULT.tool
secured_tool = tool  # the name before 0.12; kept so existing code keeps working

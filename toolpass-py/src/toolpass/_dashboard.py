"""``toolpass dashboard``: the page over the record of toolpass tool runs, from
every MCP server and agent started with ``TOOLPASS_DASHBOARD`` set, across
processes and restarts. Read-only, on the loopback interface, behind a random
token, like the toolkit's live page."""

from __future__ import annotations

from typing import Any

from toolpass._store import Store
from toolpass._watch import _Page

__all__ = ["Dashboard"]


class Dashboard(_Page):
    """The dashboard's server over one store. ``url`` is its address."""

    kind = "dashboard"

    def __init__(self, store: Store, *, listen: str = "127.0.0.1:0") -> None:
        self.store = store
        super().__init__(listen=listen)

    def events(self, since: int = 0) -> tuple[list[dict[str, Any]], int]:
        return self.store.runs(since)

"""Fixtures shared by the framework tests."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from tests import harness
from tests.frameworks.wire_scenario import wire_github


@pytest.fixture
def wire_gh() -> Iterator[harness.Server]:
    """The fake GitHub of the wire scenario (``tests/frameworks/wire_scenario.py``):
    it answers the secured tool's read and its pull request, and records what it received."""
    with wire_github() as srv:
        yield srv

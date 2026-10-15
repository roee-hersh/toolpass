from __future__ import annotations

from collections.abc import Iterator

import pytest

from tests.toolkit.conftest import Events


@pytest.fixture
def events() -> Events:
    return Events()


@pytest.fixture(autouse=True)
def _fresh_default_toolkit() -> Iterator[None]:
    import toolpass

    toolpass.configure(audit=None)
    yield
    toolpass.configure(audit=None)

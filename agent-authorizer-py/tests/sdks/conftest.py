from __future__ import annotations

from collections.abc import Iterator

import pytest

from tests.toolkit.conftest import Events


@pytest.fixture
def events() -> Events:
    return Events()


@pytest.fixture(autouse=True)
def _fresh_default_toolkit() -> Iterator[None]:
    import agent_authorizer

    agent_authorizer.configure(audit=None)
    yield
    agent_authorizer.configure(audit=None)

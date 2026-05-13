"""Shared fixtures. Globals (REGISTRY, context) reset between tests."""

from __future__ import annotations

import pytest

from mew._registry import REGISTRY
from mew.context import clear_context


@pytest.fixture(autouse=True)
def _clean_globals():
    REGISTRY.clear()
    clear_context()
    yield
    REGISTRY.clear()
    clear_context()

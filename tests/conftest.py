"""Shared fixtures. Globals (REGISTRY, context) reset between tests."""

from __future__ import annotations

import os
import sys

import pytest

from mew._registry import REGISTRY
from mew.context import clear_context

if sys.version_info >= (3, 15):
    from mew.cpu import _unwinder

    # Attach before any memray test runs in this worker: on macOS, Tachyon can't
    # attach to a process memray has tracked. mew.cpu keeps this unwinder.
    _unwinder(os.getpid())


@pytest.fixture(autouse=True)
def _clean_globals():
    REGISTRY.clear()
    clear_context()
    yield
    REGISTRY.clear()
    clear_context()

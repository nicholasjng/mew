"""Shared fixtures. Globals (REGISTRY, context) reset between tests."""

from __future__ import annotations

import os
import sys

import pytest

from mew._registry import REGISTRY
from mew.context import clear_context
from mew.machine import gil_enabled

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


@pytest.fixture(autouse=True)
def _gil_stays_disabled():
    """Fail the test that enables the GIL (for example by importing pyinstrument),
    rather than the threaded tests that later skip in the same worker."""
    enabled = gil_enabled()
    yield
    assert enabled or not gil_enabled(), "this test enabled the GIL"

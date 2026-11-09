"""Shared fixtures and session checks."""

from __future__ import annotations

import os
import sys

import pytest

from mew._registry import REGISTRY
from mew.machine import _gil_enabled, _is_free_threaded_build


def _require_gil_disabled(when: str) -> None:
    if _is_free_threaded_build() and _gil_enabled():
        pytest.exit(
            f"Free-threaded Python has the GIL enabled ({when})",
            returncode=pytest.ExitCode.TESTS_FAILED,
        )


def pytest_sessionstart() -> None:
    _require_gil_disabled("before collection")
    if sys.version_info >= (3, 15):
        from mew.cpu import _unwinder

        # Attach before any Memray test runs because Tachyon cannot attach after Memray has tracked the process on macOS.
        # mew.cpu keeps this unwinder.
        _unwinder(os.getpid())


def pytest_collection_finish() -> None:
    _require_gil_disabled("after collection")


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_protocol(item: pytest.Item):
    _require_gil_disabled(f"before {item.nodeid}")
    result = yield
    _require_gil_disabled(f"after {item.nodeid}")
    return result


@pytest.fixture(autouse=True)
def _clean_globals():
    REGISTRY.clear()
    yield
    REGISTRY.clear()

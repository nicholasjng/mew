"""Tests for `mew._significance` (stdlib Mann-Whitney U approximation)."""

from __future__ import annotations

import pytest

from mew._significance import mannwhitney_p


def test_mannwhitney_identical_groups_is_not_significant() -> None:
    p = mannwhitney_p([10.0, 11.0, 9.0, 10.5, 9.5], [10.0, 11.0, 9.0, 10.5, 9.5])
    assert p is not None
    assert p > 0.5


def test_mannwhitney_clearly_separated_groups_is_significant() -> None:
    p = mannwhitney_p([1.0, 1.1, 0.9, 1.05, 0.95], [10.0, 10.1, 9.9, 10.05, 9.95])
    assert p is not None
    assert p < 0.02


def test_mannwhitney_empty_group_returns_none() -> None:
    assert mannwhitney_p([], [1.0, 2.0]) is None
    assert mannwhitney_p([1.0, 2.0], []) is None


def test_mannwhitney_all_tied_returns_one() -> None:
    assert mannwhitney_p([5.0, 5.0], [5.0, 5.0]) == 1.0


@pytest.mark.parametrize("a,b", [([1.0], [2.0]), ([1.0, 1.0, 1.0], [1.0])])
def test_mannwhitney_handles_tiny_samples_without_error(a: list[float], b: list[float]) -> None:
    p = mannwhitney_p(a, b)
    assert p is not None
    assert 0.0 <= p <= 1.0

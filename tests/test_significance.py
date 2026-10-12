"""Tests for `mew._significance` (stdlib Mann-Whitney U approximation).

Reference p-values are SciPy 1.18.1's
``mannwhitneyu(a, b, method="asymptotic", use_continuity=True)``.
"""

from __future__ import annotations

import pytest

from mew._significance import mannwhitney_p


def test_mannwhitney_identical_groups_is_not_significant() -> None:
    p = mannwhitney_p([10.0, 11.0, 9.0, 10.5, 9.5], [10.0, 11.0, 9.0, 10.5, 9.5])
    assert p == 1.0


def test_mannwhitney_clearly_separated_groups_is_significant() -> None:
    a, b = [1.0, 1.1, 0.9, 1.05, 0.95], [10.0, 10.1, 9.9, 10.05, 9.95]
    p = mannwhitney_p(a, b)
    assert p == pytest.approx(0.01218578035534481)
    # The continuity correction shrinks |U - mu| from either side.
    assert mannwhitney_p(b, a) == p


def test_mannwhitney_corrects_the_variance_for_ties() -> None:
    p = mannwhitney_p([1.0, 2.0, 2.0, 3.0, 3.0, 4.0], [3.0, 3.0, 4.0, 5.0, 5.0, 6.0])
    # Without the tie correction this would be 0.0374.
    assert p == pytest.approx(0.03311773333120044)


def test_mannwhitney_empty_group_returns_none() -> None:
    assert mannwhitney_p([], [1.0, 2.0]) is None
    assert mannwhitney_p([1.0, 2.0], []) is None


def test_mannwhitney_all_tied_returns_one() -> None:
    assert mannwhitney_p([5.0, 5.0], [5.0, 5.0]) == 1.0


def test_mannwhitney_handles_tiny_samples_without_error() -> None:
    # One observation per group can't show a difference.
    assert mannwhitney_p([1.0], [2.0]) == 1.0

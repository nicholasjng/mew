"""Tests for `mew._statistics.mannwhitney_p` (stdlib Mann-Whitney U approximation).

Reference p-values are SciPy 1.18.1's
``mannwhitneyu(a, b, method="asymptotic", use_continuity=True)``.
"""

from __future__ import annotations

import pytest

from mew._statistics import mannwhitney_p


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ([10.0, 11.0, 9.0, 10.5, 9.5], [10.0, 11.0, 9.0, 10.5, 9.5]),  # identical
        ([5.0, 5.0], [5.0, 5.0]),  # all tied: no rank spread at all
        ([1.0], [2.0]),  # one observation per group can't show a difference
    ],
)
def test_mannwhitney_without_evidence_is_one(a: list[float], b: list[float]) -> None:
    """`mannwhitney_p` is 1.0 when the groups give no evidence of a difference."""
    assert mannwhitney_p(a, b) == 1.0


def test_mannwhitney_clearly_separated_groups_is_significant() -> None:
    """Well-separated groups give SciPy's p-value, the same in either argument order."""
    a, b = [1.0, 1.1, 0.9, 1.05, 0.95], [10.0, 10.1, 9.9, 10.05, 9.95]
    p = mannwhitney_p(a, b)
    assert p == pytest.approx(0.01218578035534481)
    # The continuity correction shrinks |U - mu| from either side.
    assert mannwhitney_p(b, a) == p


def test_mannwhitney_corrects_the_variance_for_ties() -> None:
    """Tied values reduce the variance, which gives SciPy's tie-corrected p-value."""
    p = mannwhitney_p([1.0, 2.0, 2.0, 3.0, 3.0, 4.0], [3.0, 3.0, 4.0, 5.0, 5.0, 6.0])
    # Without the tie correction this would be 0.0374.
    assert p == pytest.approx(0.03311773333120044)


def test_mannwhitney_empty_group_returns_none() -> None:
    """`mannwhitney_p` returns `None` when either group is empty."""
    assert mannwhitney_p([], [1.0, 2.0]) is None
    assert mannwhitney_p([1.0, 2.0], []) is None

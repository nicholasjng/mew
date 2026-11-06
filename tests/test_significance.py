"""Tests for `mew._statistics.mannwhitney_p` (stdlib Mann-Whitney U test).

Reference p-values are SciPy 1.18.1's ``mannwhitneyu(a, b, method="exact")`` for
small samples without ties, and ``method="asymptotic", use_continuity=True``
otherwise.
"""

from __future__ import annotations

from itertools import combinations
from math import comb

import pytest

from mew._statistics import _EXACT_MAX, _u_counts, mannwhitney_p


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ([10.0, 11.0, 9.0, 10.5, 9.5], [10.0, 11.0, 9.0, 10.5, 9.5]),  # identical
        ([5.0, 5.0], [5.0, 5.0]),  # all tied: no rank spread at all
    ],
)
def test_mannwhitney_without_evidence_is_one(a: list[float], b: list[float]) -> None:
    """`mannwhitney_p` is 1.0 when the groups give no evidence of a difference."""
    assert mannwhitney_p(a, b) == 1.0


def test_mannwhitney_clearly_separated_groups_is_significant() -> None:
    """Well-separated groups give SciPy's exact p-value, the same in either argument order."""
    a, b = [1.0, 1.1, 0.9, 1.05, 0.95], [10.0, 10.1, 9.9, 10.05, 9.95]
    p = mannwhitney_p(a, b)
    assert p == pytest.approx(2 / comb(10, 5))  # 0.0079; the approximation gives 0.0122
    assert mannwhitney_p(b, a) == p


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [
        ([3.1, 2.2, 5.3, 4.4], [4.0, 6.1, 5.9, 7.2, 8.3, 6.6], 0.0380952380952381),
        ([1.0, 2.0, 3.0], [1.5, 2.5, 3.5], 0.7),
        (
            [0.5, 2.7, 1.3, 4.2, 3.3, 6.0, 5.1],
            [2.1, 3.9, 6.4, 7.7, 5.6, 8.8, 9.0, 4.8],
            0.054079254079254076,
        ),
    ],
)
def test_mannwhitney_is_exact_for_small_samples_without_ties(a, b, expected) -> None:
    """Small samples without ties give SciPy's exact p-value."""
    assert mannwhitney_p(a, b) == pytest.approx(expected)


@pytest.mark.parametrize(("n1", "n2"), [(1, 1), (2, 5), (4, 4), (6, 3), (7, 7)])
def test_u_counts_match_enumeration(n1: int, n2: int) -> None:
    """The U distribution matches counting every rank order."""
    counts = [0] * (n1 * n2 + 1)
    for ranks in combinations(range(1, n1 + n2 + 1), n1):
        counts[sum(ranks) - n1 * (n1 + 1) // 2] += 1
    assert _u_counts(n1, n2) == counts


def test_mannwhitney_approximates_past_the_exact_size() -> None:
    """Past `_EXACT_MAX` values per sample the p-value is the normal approximation."""
    n = _EXACT_MAX + 1
    a, b = [float(i) for i in range(n)], [float(i) + 0.5 for i in range(n)]
    # SciPy's asymptotic p-value for 21 values per side, offset by half a step.
    assert mannwhitney_p(a, b) == pytest.approx(0.8013831883084928)


def test_mannwhitney_corrects_the_variance_for_ties() -> None:
    """Tied values reduce the variance, which gives SciPy's tie-corrected p-value."""
    p = mannwhitney_p([1.0, 2.0, 2.0, 3.0, 3.0, 4.0], [3.0, 3.0, 4.0, 5.0, 5.0, 6.0])
    # Without the tie correction this would be 0.0374.
    assert p == pytest.approx(0.03311773333120044)

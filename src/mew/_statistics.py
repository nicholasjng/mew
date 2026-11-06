"""Reducers for repetition values, and a Mann-Whitney U test."""

from __future__ import annotations

import math
import statistics
from collections.abc import Callable
from itertools import groupby
from typing import SupportsFloat

# Any float-castable result, e.g. a numpy scalar; callers convert it with float().
Statistic = Callable[[list[float]], SupportsFloat]


_BUILTIN_STATISTICS: dict[str, Statistic] = {
    "min": min,
    "max": max,
    "mean": statistics.fmean,
    "median": statistics.median,
    "gmean": statistics.geometric_mean,
}


def resolve_statistic(spec: str) -> Statistic:
    """Resolve a statistic name to a reducer callable.

    ``min``, ``max``, ``mean``, ``median`` or ``gmean``; raises :class:`ValueError`
    for anything else. Percentiles are left out: over the few repetitions a run
    has, they only interpolate between its largest values.
    """
    if spec in _BUILTIN_STATISTICS:
        return _BUILTIN_STATISTICS[spec]
    raise ValueError(
        f"statistic {spec!r}: unknown name; choose from {', '.join(sorted(_BUILTIN_STATISTICS))}"
    )


def _average_ranks(values: list[float]) -> tuple[list[float], float]:
    """Rank ``values`` ascending (1-based), averaging ties.

    Returns ``(ranks, tie_term)``: ``tie_term`` is ``sum(t**3 - t)`` per tie
    group of size ``t``, the variance correction term.
    """
    order = sorted(range(len(values)), key=values.__getitem__)
    ranks = [0.0] * len(values)
    tie_term = 0.0
    next_rank = 1
    for _, group in groupby(order, key=values.__getitem__):
        tied = list(group)
        count = len(tied)
        avg_rank = next_rank + (count - 1) / 2
        for i in tied:
            ranks[i] = avg_rank
        tie_term += count**3 - count
        next_rank += count
    return ranks, tie_term


#: Both samples at most this size and no ties: the exact null distribution, at
#: most 20 polynomial steps of 401 terms. Past it the normal approximation is
#: close, and exact tables would grow with every comparison row.
_EXACT_MAX = 20


def _u_counts(n1: int, n2: int) -> list[int]:
    """How many of the ``C(n1 + n2, n1)`` rank orders give each U, from 0 to ``n1 * n2``.

    The counts are the coefficients of the Gaussian binomial
    ``[n1 + n2 choose n1]_q``, built as the product over ``i`` of
    ``(1 - q**(n2 + i)) / (1 - q**i)``; every partial product is a polynomial.
    """
    poly = [1]
    for i in range(1, n1 + 1):
        shift = n2 + i
        poly += [0] * shift
        for j in range(len(poly) - 1, shift - 1, -1):  # times (1 - q**shift)
            poly[j] -= poly[j - shift]
        for j in range(i, len(poly)):  # divided by (1 - q**i)
            poly[j] += poly[j - i]
        del poly[i * n2 + 1 :]  # the quotient's degree; the rest is zero
    return poly


def mannwhitney_p(a: list[float], b: list[float]) -> float:
    """Return a two-sided p-value; compare passes two or more values per sample.

    Without ties and with at most ``_EXACT_MAX`` values per sample, the p-value
    is exact: the share of rank orders at least as extreme as the observed one.
    Otherwise it is the normal approximation with tie and continuity corrections.
    """
    n1, n2 = len(a), len(b)
    ranks, tie_term = _average_ranks([*a, *b])
    u1 = sum(ranks[:n1]) - n1 * (n1 + 1) / 2
    if not tie_term and max(n1, n2) <= _EXACT_MAX:
        counts = _u_counts(n1, n2)
        # Without ties U is a whole number, and its distribution is symmetric.
        tail = sum(counts[: round(min(u1, n1 * n2 - u1)) + 1])
        return min(1.0, 2 * tail / sum(counts))
    n = n1 + n2
    mu = n1 * n2 / 2
    sigma2 = (n1 * n2 / 12) * ((n + 1) - tie_term / (n * (n - 1)))
    if sigma2 <= 0:
        # No spread across combined ranks: no evidence of a difference, i.e. p=1.0.
        return 1.0
    sigma = math.sqrt(sigma2)
    diff = u1 - mu
    correction = 0.5 if diff > 0 else -0.5 if diff < 0 else 0.0
    z = (diff - correction) / sigma
    # 2 * (1 - Phi(|z|)), via erfc to stay accurate for tiny p.
    return math.erfc(abs(z) / math.sqrt(2))

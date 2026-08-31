"""A tie- and continuity-corrected Mann-Whitney U test."""

from __future__ import annotations

import math


def _average_ranks(values: list[float]) -> tuple[list[float], float]:
    """Rank ``values`` ascending (1-based), averaging ties.

    Returns ``(ranks, tie_term)``: ``tie_term`` is ``sum(t**3 - t)`` per tie
    group of size ``t``, the variance correction term.
    """
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    tie_term = 0.0
    n = len(values)
    i = 0
    while i < n:
        j = i
        while j + 1 < n and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg_rank = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = avg_rank
        t = j - i + 1
        if t > 1:
            tie_term += t**3 - t
        i = j + 1
    return ranks, tie_term


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2)))


def mannwhitney_p(a: list[float], b: list[float]) -> float | None:
    """Return a two-sided p-value, or ``None`` if either sample is empty."""
    n1, n2 = len(a), len(b)
    if n1 == 0 or n2 == 0:
        return None
    ranks, tie_term = _average_ranks([*a, *b])
    u1 = sum(ranks[:n1]) - n1 * (n1 + 1) / 2
    n = n1 + n2
    mu = n1 * n2 / 2
    sigma2 = (n1 * n2 / 12) * ((n + 1) - tie_term / (n * (n - 1))) if n > 1 else 0.0
    if sigma2 <= 0:
        # No spread across combined ranks: no evidence of a difference, i.e. p=1.0.
        return 1.0
    sigma = math.sqrt(sigma2)
    diff = u1 - mu
    correction = 0.5 if diff > 0 else -0.5 if diff < 0 else 0.0
    z = (diff - correction) / sigma
    p = 2.0 * (1.0 - _norm_cdf(abs(z)))
    return min(1.0, max(0.0, p))

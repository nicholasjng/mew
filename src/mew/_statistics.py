"""Reducers for repetition values, and a Mann-Whitney U test."""

from __future__ import annotations

import math
import re
import statistics
from collections.abc import Callable
from itertools import groupby
from typing import SupportsFloat

# Any float-castable result, e.g. a numpy scalar; callers convert it with float().
Statistic = Callable[[list[float]], SupportsFloat]


def _percentile(q: int) -> Statistic:
    """A stdlib percentile reducer for ``q`` in 0-100 (linear interpolation)."""

    def reduce(values: list[float]) -> float:
        if q <= 0:
            return float(min(values))
        if q >= 100:
            return float(max(values))
        if len(values) == 1:
            return float(values[0])
        # `inclusive` matches numpy.percentile; cut points p1..p99 are indices 0..98.
        return float(statistics.quantiles(values, n=100, method="inclusive")[q - 1])

    return reduce


_BUILTIN_STATISTICS: dict[str, Statistic] = {
    "min": min,
    "max": max,
    "mean": statistics.fmean,
    "median": statistics.median,
    "gmean": statistics.geometric_mean,
}

_PERCENTILE_RE = re.compile(r"p(\d{1,3})")


def resolve_statistic(spec: str) -> Statistic:
    """Resolve a statistic name to a reducer callable.

    ``min``/``max``/``mean``/``median``/``gmean``, or a ``pNN`` percentile.
    Raises :class:`ValueError` for anything else.
    """
    if spec in _BUILTIN_STATISTICS:
        return _BUILTIN_STATISTICS[spec]
    if m := _PERCENTILE_RE.fullmatch(spec):
        q = int(m.group(1))
        if q > 100:
            raise ValueError(f"statistic {spec!r}: percentile must be between 0 and 100")
        return _percentile(q)
    raise ValueError(
        f"statistic {spec!r}: unknown name; choose from "
        f"{', '.join(sorted(_BUILTIN_STATISTICS))}, or a pNN percentile like p95."
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


def mannwhitney_p(a: list[float], b: list[float]) -> float | None:
    """Return a two-sided p-value, or ``None`` if either sample is empty."""
    n1, n2 = len(a), len(b)
    if n1 == 0 or n2 == 0:
        return None
    ranks, tie_term = _average_ranks([*a, *b])
    u1 = sum(ranks[:n1]) - n1 * (n1 + 1) / 2
    n = n1 + n2
    mu = n1 * n2 / 2
    sigma2 = (n1 * n2 / 12) * ((n + 1) - tie_term / (n * (n - 1)))  # n >= 2 here
    if sigma2 <= 0:
        # No spread across combined ranks: no evidence of a difference, i.e. p=1.0.
        return 1.0
    sigma = math.sqrt(sigma2)
    diff = u1 - mu
    correction = 0.5 if diff > 0 else -0.5 if diff < 0 else 0.0
    z = (diff - correction) / sigma
    # 2 * (1 - Phi(|z|)), via erfc to stay accurate for tiny p.
    return math.erfc(abs(z) / math.sqrt(2))

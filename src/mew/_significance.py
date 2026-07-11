"""Mann-Whitney U significance test for ``mew compare``, stdlib-only.

A rank-sum test answers "could these two repetition samples plausibly come from
the same distribution," which is what turns a raw delta into "real" vs. "noise."
scipy's ``mannwhitneyu`` is the reference implementation; this is a normal-approximation
proxy (with tie and continuity correction, same formula scipy's ``method="asymptotic"``
uses) so the comparison story doesn't gain a hard dependency. Accurate enough to flag
"probably noise" at typical repetition counts (5-20); not a substitute for an exact
test at very small n, where it's simply low-power rather than wrong.
"""

from __future__ import annotations

import math


def _average_ranks(values: list[float]) -> tuple[list[float], float]:
    """Rank ``values`` ascending (1-based), averaging ranks within tie groups.

    Returns ``(ranks, tie_term)`` where ``tie_term`` is ``sum(t**3 - t)`` over
    each tie group of size ``t``, the correction term the normal approximation's
    variance needs.
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
    """Two-sided p-value for the null "``a`` and ``b`` are the same distribution."

    ``None`` when either group is empty. A p-value near 1.0 means the delta between
    the two groups is indistinguishable from repetition noise at this sample size;
    a small p-value (conventionally < 0.05) means it probably isn't.
    """
    n1, n2 = len(a), len(b)
    if n1 == 0 or n2 == 0:
        return None
    ranks, tie_term = _average_ranks([*a, *b])
    u1 = sum(ranks[:n1]) - n1 * (n1 + 1) / 2
    n = n1 + n2
    mu = n1 * n2 / 2
    sigma2 = (n1 * n2 / 12) * ((n + 1) - tie_term / (n * (n - 1))) if n > 1 else 0.0
    if sigma2 <= 0:
        # No spread in the combined ranks (identical values throughout): no
        # evidence of a difference, which *is* p=1.0, not an undefined result.
        return 1.0
    sigma = math.sqrt(sigma2)
    diff = u1 - mu
    correction = 0.5 if diff > 0 else -0.5 if diff < 0 else 0.0
    z = (diff - correction) / sigma
    p = 2.0 * (1.0 - _norm_cdf(abs(z)))
    return min(1.0, max(0.0, p))

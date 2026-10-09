"""Built-in reducers for ``mew compare``."""

from __future__ import annotations

import re
import statistics
from collections.abc import Callable

Statistic = Callable[[list[float]], float]


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

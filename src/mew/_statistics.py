"""Custom-statistic reducers for ``mew compare``.

A :data:`Statistic` reduces a benchmark's per-repetition values to one scalar.
:func:`resolve_statistic` accepts a built-in name; every reducer is handed a
``list[float]``. The built-ins are numpy-free and cover the cases a comparison
needs -- a plugin hook for arbitrary importable reducers would add a class of
runtime failures (bad import, non-callable, wrong signature) to a CLI flag.
"""

from __future__ import annotations

import re
import statistics
from collections.abc import Callable

Statistic = Callable[[list[float]], float]


def _min(values: list[float]) -> float:
    return float(min(values))


def _max(values: list[float]) -> float:
    return float(max(values))


def _mean(values: list[float]) -> float:
    return statistics.fmean(values)


def _median(values: list[float]) -> float:
    return float(statistics.median(values))


def _gmean(values: list[float]) -> float:
    return statistics.geometric_mean(values)


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
    "min": _min,
    "max": _max,
    "mean": _mean,
    "median": _median,
    "gmean": _gmean,
}

_PERCENTILE_RE = re.compile(r"p(\d{1,3})")


def reduce_statistic(statistic: Statistic, values: list[float]) -> float:
    """Apply a resolved statistic to per-repetition ``values``, casting to ``float``."""
    return float(statistic(values))


def resolve_statistic(spec: str) -> Statistic:
    """Resolve a statistic name to a reducer callable.

    ``min``/``max``/``mean``/``median``/``gmean``, or a ``pNN`` percentile.
    """
    if spec in _BUILTIN_STATISTICS:
        return _BUILTIN_STATISTICS[spec]
    if m := _PERCENTILE_RE.fullmatch(spec):
        q = int(m.group(1))
        if q > 100:
            raise SystemExit(f"statistic {spec!r}: percentile must be between 0 and 100")
        return _percentile(q)
    raise SystemExit(
        f"statistic {spec!r}: unknown name; choose from "
        f"{', '.join(sorted(_BUILTIN_STATISTICS))}, or a pNN percentile like p95."
    )

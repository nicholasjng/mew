"""Built-in reducers for `mew compare --statistic` and the aggregation they feed."""

from __future__ import annotations

from typing import Any

import numpy as np
import pytest

from mew._results import _aggregate_values
from mew._statistics import resolve_statistic


def test_statistic_replaces_the_median_but_not_the_stddev() -> None:
    """A custom statistic replaces the median as a `float`; the stddev does not change."""
    values = [1.0, 2.0, 3.0, 100.0]
    median, stddev = _aggregate_values(values)
    assert median == 2.5

    def numpy_max(values: list[float]) -> Any:
        # np.float32, unlike np.float64, is not a float subclass, so this
        # catches a dropped cast.
        return np.float32(max(values))

    center, same_stddev = _aggregate_values(values, numpy_max)
    assert (type(center), center, same_stddev) == (float, 100.0, stddev)


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        # Skewed, so mean, median and gmean all differ.
        ("min", 1.0),
        ("max", 9.0),
        ("median", 2.0),
        ("mean", 4.0),
        ("gmean", 18.0 ** (1 / 3)),
        # mew's percentile: min and max at the edges, numpy's linear
        # interpolation between them (rank 0.9 * 2 = 1.8, so 2 + 0.8 * 7).
        ("p0", 1.0),
        ("p90", 7.6),
        ("p100", 9.0),
    ],
)
def test_resolve_statistic_builtin_names(spec: str, expected: float) -> None:
    """`resolve_statistic` resolves `min`, `max`, `median`, `mean`, `gmean` and `pN` percentiles."""
    assert resolve_statistic(spec)([1.0, 2.0, 9.0]) == pytest.approx(expected)


def test_resolve_statistic_rejects_unknown_names() -> None:
    """`resolve_statistic` rejects every name that is not a built-in."""
    # Only the built-ins resolve: no stdlib fallback, no importable references.
    for spec in ("stdev", "statistics:stdev", "numpy:median"):
        with pytest.raises(ValueError, match="unknown name"):
            resolve_statistic(spec)

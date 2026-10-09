"""Built-in reducers for `mew compare --statistic` and the aggregation they feed."""

from __future__ import annotations

from typing import Any

import numpy as np
import pytest

from mew._results import _aggregate_values
from mew._statistics import resolve_statistic


def test_aggregate_values_custom_statistic_replaces_center() -> None:
    values = [1.0, 2.0, 3.0, 100.0]
    median, base_stddev = _aggregate_values(values)
    center, stddev = _aggregate_values(values, np.max)
    assert median == 2.5
    assert center == 100.0
    assert stddev == base_stddev


def test_aggregate_values_custom_statistic_gets_list() -> None:
    seen: list[object] = []

    def reduce(a):
        seen.append(a)
        return sum(a) / len(a)

    values = [2.0, 4.0]
    center, _ = _aggregate_values(values, reduce)
    assert center == 3.0
    # Reducers get a plain list, which numpy/scipy accept.
    assert seen[0] == [2.0, 4.0]
    assert isinstance(seen[0], list)


def test_aggregate_values_casts_statistic_result_to_float() -> None:
    # np.float32, unlike np.float64, is not a float subclass, so this catches a
    # dropped cast.
    def reduce(values: list[float]) -> Any:  # like a numpy reducer
        return np.float32(max(values))

    center, _ = _aggregate_values([1.0, 2.0, 3.0], reduce)
    assert type(center) is float
    assert center == 3.0


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ("min", 1.0),
        ("max", 9.0),
        ("median", 5.0),
        ("mean", 5.0),
        ("p50", 5.0),
        ("p100", 9.0),
        ("p0", 1.0),
    ],
)
def test_resolve_statistic_builtin_names(spec: str, expected: float) -> None:
    stat = resolve_statistic(spec)
    assert stat([1.0, 5.0, 9.0]) == expected


def test_resolve_statistic_percentile_picks_tail() -> None:
    stat = resolve_statistic("p90")
    # 90th percentile of 1..10 (linear interpolation) is 9.1.
    assert stat([float(i) for i in range(1, 11)]) == pytest.approx(9.1)


def test_resolve_statistic_rejects_unknown_names() -> None:
    # Only the built-ins resolve: no stdlib fallback, no importable references.
    for spec in ("stdev", "statistics:stdev", "numpy:median"):
        with pytest.raises(ValueError, match="unknown name"):
            resolve_statistic(spec)

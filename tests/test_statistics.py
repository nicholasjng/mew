"""Built-in reducers for `mew compare --statistic` and the aggregation they feed."""

from __future__ import annotations

import numpy as np
import pytest

from mew._results import _aggregate_values
from mew._statistics import reduce_statistic, resolve_statistic


def test_aggregate_values_median_and_stddev() -> None:
    values = [5.0, 7.0, 6.0]
    median, stddev = _aggregate_values(values)
    assert median == 6.0
    assert stddev is not None and stddev > 0


def test_aggregate_values_custom_statistic_replaces_center() -> None:
    # max(1,2,3,100)=100 instead of the median 2.5; stddev is unchanged.
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
    # mew hands every reducer the raw per-repetition list (numpy/scipy accept it).
    assert seen[0] == [2.0, 4.0]
    assert isinstance(seen[0], list)


def test_reduce_statistic_casts_result_to_float() -> None:
    # A numpy scalar return is fine — `reduce_statistic` casts with float(...).
    out = reduce_statistic(lambda a: np.percentile(a, 95), [1.0, 2.0, 3.0])
    assert isinstance(out, float)


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
    assert reduce_statistic(stat, [1.0, 5.0, 9.0]) == expected


def test_resolve_statistic_percentile_picks_tail() -> None:
    stat = resolve_statistic("p90")
    # 90th percentile of 1..10 (linear interpolation) is 9.1.
    assert reduce_statistic(stat, [float(i) for i in range(1, 11)]) == pytest.approx(9.1)


def test_resolve_statistic_rejects_unknown_names() -> None:
    # Only the built-ins resolve: no stdlib fallback, no importable references.
    for spec in ("stdev", "statistics:stdev", "numpy:median"):
        with pytest.raises(SystemExit, match="unknown name"):
            resolve_statistic(spec)

"""Built-in reducers for `mew compare --statistic` and the aggregation they feed."""

from __future__ import annotations

import statistics
from fractions import Fraction
from pathlib import Path

import pytest
from _helpers import row, write_json

from mew._results import load_column
from mew._statistics import resolve_statistic


def test_statistic_replaces_the_median_but_not_the_stddev(tmp_path: Path) -> None:
    """A custom statistic replaces the median as a `float`; the stddev does not change."""
    values = [1.0, 2.0, 3.0, 100.0]
    write_json(tmp_path / "r.json", [row("b", v) for v in values])
    (median,) = load_column(tmp_path / "r.json", "real_time")[0].values()
    assert median.value == 2.5
    # A Fraction, like a numpy scalar, is no float subclass, so this catches a dropped cast.
    samples, _ = load_column(tmp_path / "r.json", "real_time", statistic=lambda v: Fraction(max(v)))
    (center,) = samples.values()
    assert (type(center.value), center.value) == (float, 100.0)
    assert center.stddev == median.stddev == statistics.stdev(values)


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        # Skewed, so mean, median and gmean all differ.
        ("min", 1.0),
        ("max", 9.0),
        ("median", 2.0),
        ("mean", 4.0),
        ("gmean", 18.0 ** (1 / 3)),
    ],
)
def test_resolve_statistic_builtin_names(spec: str, expected: float) -> None:
    """`resolve_statistic` resolves `min`, `max`, `median`, `mean` and `gmean`."""
    assert resolve_statistic(spec)([1.0, 2.0, 9.0]) == pytest.approx(expected)


def test_resolve_statistic_rejects_unknown_names() -> None:
    """`resolve_statistic` rejects every name that is not a built-in."""
    # Only the built-ins resolve: no stdlib fallback, no importable references.
    for spec in ("stdev", "p95", "statistics:stdev", "numpy:median"):
        with pytest.raises(ValueError, match="unknown name"):
            resolve_statistic(spec)

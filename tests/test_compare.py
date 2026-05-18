"""Tests for `mew.compare`."""

from __future__ import annotations

import json
from importlib.util import find_spec
from pathlib import Path

import pytest
from rich.console import Console

from mew.compare import _aggregate_group, _load, compare


def _make_doc(benches: list[dict]) -> dict:
    return {"context": {}, "benchmarks": benches}


def _row(name: str, real_time: float, **extra) -> dict:
    return {
        "name": name,
        "real_time": real_time,
        "cpu_time": real_time,
        "iterations": 1000,
        "time_unit": "ns",
        "aggregate_name": "",
        **extra,
    }


def _write_json(path: Path, benches: list[dict]) -> None:
    path.write_text(json.dumps(_make_doc(benches)))


def test_load_basic(tmp_path: Path) -> None:
    p = tmp_path / "a.json"
    _write_json(p, [_row("bench_x", 10.0)])
    samples = _load(p, "real_time")
    assert set(samples) == {"bench_x"}
    assert samples["bench_x"].value == 10.0
    assert samples["bench_x"].time_unit == "ns"


def test_aggregate_group_median_and_stddev() -> None:
    rows = [_row("b", 5.0), _row("b", 7.0), _row("b", 6.0)]
    median, stddev = _aggregate_group(rows, "real_time")
    assert median == 6.0
    assert stddev is not None and stddev > 0


def test_load_ignores_gb_aggregate_rows(tmp_path: Path) -> None:
    p = tmp_path / "a.json"
    # If we read the aggregate row, we'd see 999; we should compute median of [5,7] = 6.
    _write_json(
        p,
        [
            _row("b", 5.0),
            _row("b", 7.0),
            _row("b", 999.0, aggregate_name="median"),
        ],
    )
    samples = _load(p, "real_time")
    assert samples["b"].value == 6.0
    assert samples["b"].stddev is not None


def test_compare_speedup_signs(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    base = tmp_path / "base.json"
    other = tmp_path / "other.json"
    _write_json(base, [_row("bench_x", 100.0), _row("bench_y", 50.0)])
    _write_json(other, [_row("bench_x", 80.0), _row("bench_y", 75.0)])

    console = Console(record=True, width=200)
    code = compare([base, other], console=console)
    assert code == 0
    out = console.export_text()
    # bench_x faster (-20%, ×1.25); bench_y slower (+50%, ×0.667).
    assert "-20.00%" in out
    assert "×1.250" in out
    assert "+50.00%" in out
    assert "×0.667" in out


def test_compare_warns_on_missing_names(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    base = tmp_path / "base.json"
    other = tmp_path / "other.json"
    _write_json(base, [_row("bench_x", 100.0), _row("bench_only_in_base", 1.0)])
    _write_json(other, [_row("bench_x", 80.0)])

    console = Console(record=True, width=200)
    code = compare([base, other], console=console)
    assert code == 0
    err = capsys.readouterr().err
    assert "bench_only_in_base" in err
    out = console.export_text()
    # Overlap rendered, missing benchmark skipped.
    assert "bench_x" in out
    assert "bench_only_in_base" not in out


def test_compare_empty_overlap(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    base = tmp_path / "base.json"
    other = tmp_path / "other.json"
    _write_json(base, [_row("a", 1.0)])
    _write_json(other, [_row("b", 1.0)])
    code = compare([base, other], console=Console(record=True))
    assert code == 1
    assert "no overlapping benchmarks" in capsys.readouterr().err


def test_compare_pattern_filter(tmp_path: Path) -> None:
    base = tmp_path / "base.json"
    other = tmp_path / "other.json"
    _write_json(base, [_row("alpha", 10.0), _row("beta", 20.0)])
    _write_json(other, [_row("alpha", 5.0), _row("beta", 40.0)])
    console = Console(record=True, width=200)
    code = compare([base, other], pattern="alpha", console=console)
    assert code == 0
    out = console.export_text()
    assert "alpha" in out
    assert "beta" not in out


def test_compare_requires_two_files(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="at least two"):
        compare([tmp_path / "a.json"])


def test_compare_unknown_metric(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="unknown metric"):
        compare([tmp_path / "a.json", tmp_path / "b.json"], metric="bogus")


def test_compare_stddev_column(tmp_path: Path) -> None:
    base = tmp_path / "base.json"
    other = tmp_path / "other.json"
    _write_json(base, [_row("b", 95.0), _row("b", 100.0), _row("b", 105.0)])
    _write_json(other, [_row("b", 78.0), _row("b", 80.0), _row("b", 82.0)])
    console = Console(record=True, width=200)
    code = compare([base, other], show_stddev=True, console=console)
    assert code == 0
    out = console.export_text()
    # Stdlib stdev([95,100,105]) = 5.0; stdev([78,80,82]) = 2.0
    assert "5.00" in out
    assert "2.00" in out


def test_load_multi_session_keeps_latest(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Simulate a concatenated Parquet by hand-building rows with per-row dates.
    p = tmp_path / "agg.json"
    _write_json(
        p,
        [
            _row("b", 10.0, date="2026-01-01T00:00:00", host_name="h1"),
            _row("b", 20.0, date="2026-05-01T00:00:00", host_name="h2"),
        ],
    )
    samples = _load(p, "real_time")
    assert samples["b"].value == 20.0
    err = capsys.readouterr().err
    assert "2 sessions" in err
    assert "2026-05-01" in err


@pytest.mark.skipif(find_spec("pyarrow") is None, reason="pyarrow not installed")
def test_compare_parquet_roundtrip(tmp_path: Path) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq

    def write(path: Path, rows: list[dict]) -> None:
        pq.write_table(pa.Table.from_pylist(rows), path)

    base = tmp_path / "base.parquet"
    other = tmp_path / "other.parquet"
    write(base, [_row("bench_x", 100.0)])
    write(other, [_row("bench_x", 50.0)])
    console = Console(record=True, width=200)
    code = compare([base, other], console=console)
    assert code == 0
    out = console.export_text()
    assert "-50.00%" in out
    assert "×2.000" in out

"""Tests for `mew.compare`."""

from __future__ import annotations

import io
import json
from importlib.util import find_spec
from pathlib import Path

import pytest

from mew._console import Terminal
from mew.compare import (
    _aggregate_group,
    _load,
    _load_sessions,
    _load_variant_columns,
    _resolve_session,
    _select_latest,
    _split_selector,
    compare,
)


class Console(Terminal):
    """A capture terminal mirroring rich's record/export_text shape, color off."""

    def __init__(self, *, record: bool = True, width: int = 80) -> None:
        self._buf = io.StringIO()
        super().__init__(file=self._buf, width=width, color=False)

    def export_text(self) -> str:
        return self._buf.getvalue()


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
    samples, _ = _load(p, "real_time")
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
    samples, _ = _load(p, "real_time")
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
    # Both absolute timings are shown, not just the baseline's.
    assert "100.00 ns" in out
    assert "80.00 ns" in out
    assert "75.00 ns" in out


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


def test_compare_pattern_is_regex(tmp_path: Path) -> None:
    base = tmp_path / "base.json"
    other = tmp_path / "other.json"
    _write_json(base, [_row("alpha", 10.0), _row("beta", 20.0), _row("gamma", 30.0)])
    _write_json(other, [_row("alpha", 5.0), _row("beta", 40.0), _row("gamma", 15.0)])
    console = Console(record=True, width=200)
    # Alternation selects two of the three; the third is filtered out.
    assert compare([base, other], pattern="alpha|gamma", console=console) == 0
    out = console.export_text()
    assert "alpha" in out
    assert "gamma" in out
    assert "beta" not in out


def test_compare_invalid_pattern_errors(tmp_path: Path) -> None:
    base = tmp_path / "base.json"
    other = tmp_path / "other.json"
    _write_json(base, [_row("alpha", 10.0)])
    _write_json(other, [_row("alpha", 5.0)])
    with pytest.raises(SystemExit, match="invalid benchmark filter pattern"):
        compare([base, other], pattern="foo(", console=Console(record=True))


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
    samples, _ = _load(p, "real_time")
    assert samples["b"].value == 20.0
    err = capsys.readouterr().err
    assert "2 sessions" in err
    assert "2026-05-01" in err


def _write_jsonl(path: Path, benches: list[dict], context: dict | None = None) -> None:
    lines = [json.dumps({"context": context or {}})]
    lines += [json.dumps(b) for b in benches]
    path.write_text("\n".join(lines) + "\n")


def test_load_jsonl(tmp_path: Path) -> None:
    p = tmp_path / "a.jsonl"
    _write_jsonl(p, [_row("bench_x", 10.0)], context={"host_name": "h1"})
    samples, ctx = _load(p, "real_time")
    assert samples["bench_x"].value == 10.0
    assert ctx == {"host_name": "h1"}


def test_load_jsonl_rejects_invalid_line(tmp_path: Path) -> None:
    p = tmp_path / "a.jsonl"
    p.write_text('{"context": {}}\nnot json\n')
    with pytest.raises(ValueError, match="invalid JSON"):
        _load(p, "real_time")


def test_compare_jsonl_files(tmp_path: Path) -> None:
    base = tmp_path / "base.jsonl"
    other = tmp_path / "other.jsonl"
    _write_jsonl(base, [_row("bench_x", 100.0)])
    _write_jsonl(other, [_row("bench_x", 50.0)])
    console = Console(record=True, width=200)
    assert compare([base, other], console=console) == 0
    assert "×2.000" in console.export_text()


def test_load_key_func_strips_file_prefix(tmp_path: Path) -> None:
    p = tmp_path / "a.json"
    _write_json(p, [_row("bench_ducky.py::bench_select", 10.0)])
    samples, _ = _load(p, "real_time", key="func")
    assert set(samples) == {"bench_select"}
    assert samples["bench_select"].name == "bench_select"


def test_load_key_func_rejects_collisions(tmp_path: Path) -> None:
    p = tmp_path / "a.json"
    _write_json(p, [_row("a.py::bench_x", 1.0), _row("b.py::bench_x", 2.0)])
    with pytest.raises(SystemExit, match="maps both"):
        _load(p, "real_time", key="func")


def test_compare_key_func_matches_across_suites(tmp_path: Path) -> None:
    # The cross-engine A/B shape: same function names, different bench files.
    base = tmp_path / "ducky.json"
    other = tmp_path / "duckdb.json"
    _write_json(base, [_row("bench_ducky.py::bench_select", 100.0)])
    _write_json(other, [_row("bench_duckdb.py::bench_select", 80.0)])

    console = Console(record=True, width=200)
    assert compare([base, other], console=Console(record=True)) == 1  # no overlap by name
    code = compare([base, other], key="func", console=console)
    assert code == 0
    out = console.export_text()
    assert "bench_select" in out
    assert "-20.00%" in out


def test_compare_unknown_key(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="unknown key"):
        compare([tmp_path / "a.json", tmp_path / "b.json"], key="bogus")


def _two_session_file(tmp_path: Path) -> Path:
    """A single JSON file holding 'before' and 'after' sessions of one benchmark."""
    p = tmp_path / "results.json"
    _write_json(
        p,
        [
            _row(
                "b",
                100.0,
                date="2026-01-01T00:00:00",
                session_id="0197aaaa11",
                session_tag="before",
            ),
            _row(
                "b", 80.0, date="2026-02-01T00:00:00", session_id="0197bbbb22", session_tag="after"
            ),
        ],
    )
    return p


def test_split_selector_plain_path(tmp_path: Path) -> None:
    p = tmp_path / "a.json"
    p.write_text("{}")
    # An existing file is never split, even if its name contains '@'.
    weird = tmp_path / "weird@name.json"
    weird.write_text("{}")
    assert _split_selector(str(p)) == (p, None)
    assert _split_selector(str(weird)) == (weird, None)


def test_split_selector_extracts_selector(tmp_path: Path) -> None:
    p = tmp_path / "results.json"
    assert _split_selector(f"{p}@before") == (p, "before")
    assert _split_selector(f"{p}@~1") == (p, "~1")
    assert _split_selector(str(p)) == (p, None)


def test_resolve_session_by_tag_and_keywords(tmp_path: Path) -> None:
    sessions = _load_sessions(_two_session_file(tmp_path), "real_time")
    assert _resolve_session(tmp_path, sessions, "before").session_tag == "before"
    assert _resolve_session(tmp_path, sessions, "latest").session_tag == "after"
    assert _resolve_session(tmp_path, sessions, "earliest").session_tag == "before"
    assert _resolve_session(tmp_path, sessions, "~1").session_tag == "before"  # one back
    assert _resolve_session(tmp_path, sessions, "~0").session_tag == "after"


def test_resolve_session_by_id_prefix(tmp_path: Path) -> None:
    sessions = _load_sessions(_two_session_file(tmp_path), "real_time")
    assert _resolve_session(tmp_path, sessions, "0197aaaa").session_tag == "before"


def test_resolve_session_errors(tmp_path: Path) -> None:
    sessions = _load_sessions(_two_session_file(tmp_path), "real_time")
    with pytest.raises(SystemExit, match="no session matching"):
        _resolve_session(tmp_path, sessions, "nope")
    with pytest.raises(SystemExit, match="out of range"):
        _resolve_session(tmp_path, sessions, "~9")


def test_resolve_session_ambiguous_tag(tmp_path: Path) -> None:
    p = tmp_path / "r.json"
    _write_json(
        p,
        [
            _row("b", 10.0, date="2026-01-01T00:00:00", session_id="aaaa", session_tag="dup"),
            _row("b", 20.0, date="2026-02-01T00:00:00", session_id="bbbb", session_tag="dup"),
        ],
    )
    sessions = _load_sessions(p, "real_time")
    with pytest.raises(SystemExit, match="ambiguous"):
        _resolve_session(p, sessions, "dup")


def test_compare_two_sessions_of_one_file(tmp_path: Path) -> None:
    p = _two_session_file(tmp_path)
    console = Console(record=True, width=200)
    code = compare([Path(f"{p}@before"), Path(f"{p}@after")], console=console)
    assert code == 0
    out = console.export_text()
    # Selector-aware labels keep the two columns distinct.
    assert "results.json@before" in out or "results@before" in out
    assert "-20.00%" in out  # 100 -> 80
    # Session shows in the provenance line.
    assert "session=before" in out
    assert "session=after" in out


def test_load_with_selector(tmp_path: Path) -> None:
    p = _two_session_file(tmp_path)
    samples, ctx = _load(p, "real_time", selector="before")
    assert samples["b"].value == 100.0
    assert ctx["session_tag"] == "before"


def test_compare_prints_context_and_warns_on_skew(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    base = tmp_path / "base.json"
    other = tmp_path / "other.json"
    base.write_text(
        json.dumps(
            {
                "context": {"host_name": "h1", "num_cpus": 8, "custom": {"engine": "ducky 0.1"}},
                "benchmarks": [_row("bench_x", 100.0)],
            }
        )
    )
    other.write_text(
        json.dumps(
            {
                "context": {"host_name": "h2", "num_cpus": 4, "custom": {"engine": "duckdb 1.5.3"}},
                "benchmarks": [_row("bench_x", 80.0)],
            }
        )
    )
    console = Console(record=True, width=200)
    assert compare([base, other], console=console) == 0
    out = console.export_text()
    # Per-file provenance headers.
    assert "host=h1" in out
    assert "host=h2" in out
    # Differing custom keys annotate the column labels.
    assert "engine=ducky 0.1" in out
    assert "engine=duckdb 1.5.3" in out
    err = capsys.readouterr().err
    assert "host_name" in err
    assert "num_cpus" in err


def test_compare_no_skew_warning_when_contexts_match(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    base = tmp_path / "base.json"
    other = tmp_path / "other.json"
    ctx = {"host_name": "h1", "num_cpus": 8}
    base.write_text(json.dumps({"context": ctx, "benchmarks": [_row("bench_x", 100.0)]}))
    other.write_text(json.dumps({"context": ctx, "benchmarks": [_row("bench_x", 80.0)]}))
    assert compare([base, other], console=Console(record=True, width=200)) == 0
    assert "differ in" not in capsys.readouterr().err


def test_load_renders_case_rows_by_label(tmp_path: Path) -> None:
    p = tmp_path / "a.json"
    _write_json(
        p,
        [
            _row("bench.py::bench_udf/case:0/min_time:0.200", 10.0, label="n=100"),
            _row("bench.py::bench_udf/case:1/min_time:0.200", 20.0, label="n=10000"),
        ],
    )
    samples, _ = _load(p, "real_time")
    assert set(samples) == {"bench.py::bench_udf[n=100]", "bench.py::bench_udf[n=10000]"}


def test_load_keeps_case_index_without_label(tmp_path: Path) -> None:
    p = tmp_path / "a.json"
    _write_json(p, [_row("bench.py::bench_udf/case:0", 10.0, label="")])
    samples, _ = _load(p, "real_time")
    assert set(samples) == {"bench.py::bench_udf/case:0"}


def test_load_ignores_label_on_plain_benchmarks(tmp_path: Path) -> None:
    # set_label() on a non-parametrized benchmark is informational, not identity.
    p = tmp_path / "a.json"
    _write_json(p, [_row("bench.py::bench_x", 10.0, label="some note")])
    samples, _ = _load(p, "real_time")
    assert set(samples) == {"bench.py::bench_x"}


def test_load_strips_option_suffix_chains(tmp_path: Path) -> None:
    p = tmp_path / "a.json"
    _write_json(p, [_row("bench.py::bench_x/min_time:0.200/repeats:3/real_time", 10.0)])
    samples, _ = _load(p, "real_time")
    assert set(samples) == {"bench.py::bench_x"}


def test_load_option_stripping_spares_path_segments(tmp_path: Path) -> None:
    # `real_time` as a path segment in the registered name is not an option suffix.
    p = tmp_path / "a.json"
    _write_json(p, [_row("suites/real_time.py::bench_x", 10.0)])
    samples, _ = _load(p, "real_time")
    assert set(samples) == {"suites/real_time.py::bench_x"}


def test_compare_aligns_files_run_with_different_min_time(tmp_path: Path) -> None:
    base = tmp_path / "base.json"
    other = tmp_path / "other.json"
    _write_json(base, [_row("a.py::bench_x/case:0/min_time:0.200", 100.0, label="n=10")])
    _write_json(other, [_row("a.py::bench_x/case:0/min_time:0.500", 80.0, label="n=10")])
    console = Console(record=True, width=200)
    assert compare([base, other], console=console) == 0
    out = console.export_text()
    assert "a.py::bench_x[n=10]" in out
    assert "-20.00%" in out


def _variant_file(tmp_path: Path) -> Path:
    """A single result file with two variants 'a' and 'b' of one benchmark."""
    p = tmp_path / "variants.json"
    _write_json(
        p,
        [
            _row("bench.py::f", 100.0, variant="a", session_id="s1", date="2026-01-01T00:00:00"),
            _row("bench.py::f", 80.0, variant="b", session_id="s1", date="2026-01-01T00:00:00"),
        ],
    )
    return p


def test_load_variant_columns_pivots(tmp_path: Path) -> None:
    cols = _load_variant_columns(_variant_file(tmp_path), "real_time", "name")
    assert [v for v, _, _ in cols] == ["a", "b"]  # first-encounter order
    by = {v: s for v, s, _ in cols}
    assert by["a"]["bench.py::f"].value == 100.0
    assert by["b"]["bench.py::f"].value == 80.0


def test_compare_by_variant(tmp_path: Path) -> None:
    console = Console(record=True, width=200)
    code = compare([_variant_file(tmp_path)], by="variant", console=console)
    assert code == 0
    out = console.export_text()
    assert "a (baseline)" in out
    assert "-20.00%" in out  # b is 20% faster than a


def test_compare_by_variant_baseline(tmp_path: Path) -> None:
    console = Console(record=True, width=200)
    code = compare([_variant_file(tmp_path)], by="variant", baseline="b", console=console)
    assert code == 0
    out = console.export_text()
    assert "b (baseline)" in out
    assert "+25.00%" in out  # a is 25% slower than b


def test_compare_by_variant_unknown_baseline(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="not among variants"):
        compare([_variant_file(tmp_path)], by="variant", baseline="zzz")


def test_compare_by_variant_requires_single_file(tmp_path: Path) -> None:
    p = _variant_file(tmp_path)
    with pytest.raises(SystemExit, match="exactly one"):
        compare([p, p], by="variant")


def test_compare_by_variant_no_variant_data(tmp_path: Path) -> None:
    p = tmp_path / "plain.json"
    _write_json(p, [_row("bench.py::f", 1.0)])  # no 'variant' field
    with pytest.raises(SystemExit, match="no 'variant' data"):
        compare([p], by="variant")


def test_compare_unknown_by(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="unknown --by"):
        compare([tmp_path / "a.json"], by="bogus")


def _mem_row(name: str, real_time: float, peak: int, allocs: int, *, iterations: int = 1) -> dict:
    return _row(
        name,
        real_time,
        memory={
            "profiler": "memray",
            "peak_bytes": peak,
            "total_bytes": peak,
            "total_allocations": allocs,
            "iterations": iterations,
            "allocations_per_iteration": allocs / iterations,
        },
    )


def test_compare_memory_metric(tmp_path: Path) -> None:
    base = tmp_path / "base.json"
    other = tmp_path / "other.json"
    _write_json(base, [_mem_row("bench_x", 1.0, peak=1 << 20, allocs=100)])
    _write_json(other, [_mem_row("bench_x", 1.0, peak=1 << 21, allocs=50)])

    console = Console(record=True, width=200)
    code = compare([base, other], metric="memory.peak_bytes", console=console)
    assert code == 0
    out = console.export_text()
    assert "1.0 MB" in out  # byte-formatted baseline
    assert "+100.00%" in out

    console = Console(record=True, width=200)
    code = compare([base, other], metric="memory.total_allocations", console=console)
    assert code == 0
    assert "-50.00%" in console.export_text()


def test_compare_allocations_per_iteration_is_speed_independent(tmp_path: Path) -> None:
    # The whole point of item 0: two engines whose raw total_allocations differ
    # only because they ran a different iteration count compare *equal* per-iter.
    base = tmp_path / "base.json"
    other = tmp_path / "other.json"
    # Same per-call allocations (10), captured over different iteration counts.
    _write_json(base, [_mem_row("bench_x", 1.0, peak=1 << 20, allocs=1000, iterations=100)])
    _write_json(other, [_mem_row("bench_x", 1.0, peak=1 << 20, allocs=500, iterations=50)])

    console = Console(record=True, width=200)
    code = compare([base, other], metric="memory.allocations_per_iteration", console=console)
    assert code == 0
    out = console.export_text()
    assert "10.0" in out  # baseline per-iteration count, fractional format
    assert "+0.00%" in out  # identical per-call work despite 2× raw allocs


def test_compare_memory_metric_without_data_hints_profile_flag(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    base = tmp_path / "base.json"
    other = tmp_path / "other.json"
    _write_json(base, [_row("bench_x", 1.0)])
    _write_json(other, [_row("bench_x", 1.0)])
    code = compare([base, other], metric="memory.peak_bytes", console=Console(record=True))
    assert code == 1
    assert "--profile-memory" in capsys.readouterr().err


def test_compare_marks_high_cv_rows(tmp_path: Path) -> None:
    base = tmp_path / "base.json"
    other = tmp_path / "other.json"
    # Baseline reps scatter wildly (CV >> 25%); other is steady.
    _write_json(base, [_row("b", 40.0), _row("b", 100.0), _row("b", 160.0)])
    _write_json(other, [_row("b", 99.0), _row("b", 100.0), _row("b", 101.0)])
    console = Console(record=True, width=200)
    assert compare([base, other], console=console) == 0
    out = console.export_text()
    assert "(!)" in out
    assert "±60%" in out  # stdev([40,100,160])/median = 60/100


def test_compare_no_cv_marker_on_steady_rows(tmp_path: Path) -> None:
    base = tmp_path / "base.json"
    other = tmp_path / "other.json"
    _write_json(base, [_row("b", 99.0), _row("b", 100.0), _row("b", 101.0)])
    _write_json(other, [_row("b", 49.0), _row("b", 50.0), _row("b", 51.0)])
    console = Console(record=True, width=200)
    assert compare([base, other], console=console) == 0
    assert "(!)" not in console.export_text()


def test_load_sessions_keeps_all_sessions(tmp_path: Path) -> None:
    # The load stage discards nothing; collapsing is the select stage's job.
    p = tmp_path / "agg.json"
    _write_json(
        p,
        [
            _row("b", 10.0, date="2026-01-01T00:00:00", host_name="h1"),
            _row("b", 20.0, date="2026-05-01T00:00:00", host_name="h2"),
        ],
    )
    sessions = _load_sessions(p, "real_time")
    assert [s.date for s in sessions] == ["2026-01-01T00:00:00", "2026-05-01T00:00:00"]
    assert [s.samples["b"].value for s in sessions] == [10.0, 20.0]


def test_load_sessions_distinguishes_same_second_runs_by_session_id(tmp_path: Path) -> None:
    # The roadmap's collision case: two runs in the same wall-clock second on
    # one host. session_id keeps them apart; (date, host) alone could not.
    p = tmp_path / "agg.json"
    when = "2026-06-12T10:00:00"
    _write_json(
        p,
        [
            _row(
                "b", 10.0, date=when, host_name="h1", session_id="0197-aaaa", session_tag="before"
            ),
            _row("b", 20.0, date=when, host_name="h1", session_id="0197-bbbb", session_tag="after"),
        ],
    )
    sessions = _load_sessions(p, "real_time")
    assert [s.session_id for s in sessions] == ["0197-aaaa", "0197-bbbb"]
    assert [s.session_tag for s in sessions] == ["before", "after"]
    assert [s.samples["b"].value for s in sessions] == [10.0, 20.0]


def test_select_latest_merges_per_name(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # Latest wins per name; a benchmark only present in an older session survives.
    p = tmp_path / "agg.json"
    _write_json(
        p,
        [
            _row("shared", 10.0, date="2026-01-01T00:00:00"),
            _row("old_only", 1.0, date="2026-01-01T00:00:00"),
            _row("shared", 20.0, date="2026-05-01T00:00:00"),
        ],
    )
    samples, _ = _select_latest(p, _load_sessions(p, "real_time"))
    assert samples["shared"].value == 20.0
    assert samples["old_only"].value == 1.0
    err = capsys.readouterr().err
    assert "'shared' has 2 sessions" in err
    assert "old_only" not in err


@pytest.mark.skipif(find_spec("pyarrow") is None, reason="pyarrow not installed")
def test_compare_parquet_memory_metric_parses_json_column(tmp_path: Path) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq

    def write(path: Path, peak: int) -> None:
        row = _row("bench_x", 1.0)
        row["memory"] = json.dumps(
            {"profiler": "memray", "peak_bytes": peak, "total_bytes": peak, "total_allocations": 1}
        )
        pq.write_table(pa.Table.from_pylist([row]), path)

    base = tmp_path / "base.parquet"
    other = tmp_path / "other.parquet"
    write(base, 1 << 20)
    write(other, 1 << 19)
    console = Console(record=True, width=200)
    assert compare([base, other], metric="memory.peak_bytes", console=console) == 0
    assert "-50.00%" in console.export_text()


@pytest.mark.skipif(find_spec("pyarrow") is None, reason="pyarrow not installed")
def test_compare_parquet_skips_cpu_profile_column(tmp_path: Path) -> None:
    """A timing compare must not read/decode `cpu_profile` — it's projected away.

    The blob here is deliberately un-decodable JSON: if the reader ever touched
    it, `_decode_json_str` would surface garbage (or the column read would bloat
    memory). The compare succeeding proves the column is never materialized.
    """
    import pyarrow as pa
    import pyarrow.parquet as pq

    from mew.compare import _parquet_projection
    from mew.reporter import _parquet_schema

    schema = _parquet_schema()

    def write(path: Path, rt: float) -> None:
        row = {f.name: None for f in schema}
        row.update(
            name="pkg::bench_x",
            aggregate_name="",
            time_unit="ns",
            real_time=rt,
            cpu_time=rt,
            iterations=1000,
            counters=[("a", 1.0)],
            cpu_profile="{ NOT VALID JSON >>>",  # poison: never decoded for a timing metric
        )
        pq.write_table(pa.Table.from_pylist([row], schema=schema), path)

    base = tmp_path / "base.parquet"
    other = tmp_path / "other.parquet"
    write(base, 100.0)
    write(other, 50.0)
    assert "cpu_profile" not in _parquet_projection("real_time")
    console = Console(record=True, width=200)
    assert compare([base, other], console=console) == 0
    assert "×2.000" in console.export_text()


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

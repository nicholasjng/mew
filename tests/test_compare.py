"""Tests for `mew.compare`."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
from _helpers import (
    Console,
    row as _row,
    write_json as _write_json,
    write_jsonl as _write_jsonl,
    write_pair as _write_pair,
)

from mew._results import _aggregate_values, _load, _split_selector
from mew._statistics import reduce_statistic, resolve_statistic
from mew.compare import compare, read_results


def test_load_basic(tmp_path: Path) -> None:
    p = tmp_path / "a.json"
    _write_json(p, [_row("bench_x", 10.0)])
    samples, _ = _load(p, "real_time")
    assert set(samples) == {"bench_x"}
    assert samples["bench_x"].value == 10.0
    assert samples["bench_x"].time_unit == "ns"


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


def test_builtin_statistic_is_stdlib_backed() -> None:
    # Built-ins resolve to stdlib callables — no numpy on this path.
    stat = resolve_statistic("p95")
    assert "numpy" not in getattr(stat, "__module__", "")
    # p95 of [1,2,3] is 2.9 (linear interpolation, matching numpy.percentile).
    assert reduce_statistic(stat, [1.0, 2.0, 3.0]) == pytest.approx(2.9)


def test_compare_custom_statistic_end_to_end(tmp_path: Path) -> None:
    # Both files: a tight cluster plus one outlier. median ignores it; max picks it.
    other, base = _write_pair(
        tmp_path,
        other=[_row("b", 1.0), _row("b", 2.0), _row("b", 51.0)],
        base=[_row("b", 1.0), _row("b", 2.0), _row("b", 99.0)],
    )

    console = Console(width=200)
    code = compare([other, base], statistic=np.max, console=console)
    assert code == 0
    out = console.export_text()
    # Center is the max (99 vs 51), and the delta reflects those, not the medians.
    assert "99.00 ns" in out
    assert "51.00 ns" in out
    assert "-48.48%" in out


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
    other, base = _write_pair(
        tmp_path,
        other=[_row("bench_x", 80.0), _row("bench_y", 75.0)],
        base=[_row("bench_x", 100.0), _row("bench_y", 50.0)],
    )

    console = Console(width=200)
    code = compare([other, base], console=console)
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
    other, base = _write_pair(
        tmp_path,
        other=[_row("bench_x", 80.0)],
        base=[_row("bench_x", 100.0), _row("bench_only_in_base", 1.0)],
    )

    console = Console(width=200)
    code = compare([other, base], console=console)
    assert code == 0
    err = capsys.readouterr().err
    assert "bench_only_in_base" in err
    out = console.export_text()
    # Overlap rendered, missing benchmark skipped.
    assert "bench_x" in out
    assert "bench_only_in_base" not in out


def test_compare_empty_overlap(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    other, base = _write_pair(tmp_path, other=[_row("b", 1.0)], base=[_row("a", 1.0)])
    code = compare([other, base], console=Console())
    assert code == 1
    assert "no overlapping benchmarks" in capsys.readouterr().err


def test_compare_pattern_filter(tmp_path: Path) -> None:
    other, base = _write_pair(
        tmp_path,
        other=[_row("alpha", 5.0), _row("beta", 40.0)],
        base=[_row("alpha", 10.0), _row("beta", 20.0)],
    )
    console = Console(width=200)
    code = compare([other, base], pattern="alpha", console=console)
    assert code == 0
    out = console.export_text()
    assert "alpha" in out
    assert "beta" not in out


def test_compare_pattern_is_regex(tmp_path: Path) -> None:
    other, base = _write_pair(
        tmp_path,
        other=[_row("alpha", 5.0), _row("beta", 40.0), _row("gamma", 15.0)],
        base=[_row("alpha", 10.0), _row("beta", 20.0), _row("gamma", 30.0)],
    )
    console = Console(width=200)
    # Alternation selects two of the three; the third is filtered out.
    assert compare([other, base], pattern="alpha|gamma", console=console) == 0
    out = console.export_text()
    assert "alpha" in out
    assert "gamma" in out
    assert "beta" not in out


def test_compare_invalid_pattern_errors(tmp_path: Path) -> None:
    other, base = _write_pair(tmp_path, other=[_row("alpha", 5.0)], base=[_row("alpha", 10.0)])
    with pytest.raises(SystemExit, match="invalid benchmark filter pattern"):
        compare([other, base], pattern="foo(", console=Console())


def test_compare_requires_two_files(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="at least two"):
        compare([tmp_path / "a.json"])


def test_compare_unknown_metric(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="unknown metric"):
        compare([tmp_path / "a.json", tmp_path / "b.json"], metric="bogus")


def test_compare_stddev_column(tmp_path: Path) -> None:
    other, base = _write_pair(
        tmp_path,
        other=[_row("b", 78.0), _row("b", 80.0), _row("b", 82.0)],
        base=[_row("b", 95.0), _row("b", 100.0), _row("b", 105.0)],
    )
    console = Console(width=200)
    code = compare([other, base], show_stddev=True, console=console)
    assert code == 0
    out = console.export_text()
    # Stdlib stdev([95,100,105]) = 5.0; stdev([78,80,82]) = 2.0
    assert "5.00" in out
    assert "2.00" in out


def test_load_multi_session_keeps_latest(tmp_path: Path) -> None:
    # An archive holding several sessions contributes its newest, silently.
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


def test_load_jsonl(tmp_path: Path) -> None:
    p = tmp_path / "a.jsonl"
    _write_jsonl(p, [_row("bench_x", 10.0)], context={"session": {"host": "h1"}})
    samples, ctx = _load(p, "real_time")
    assert samples["bench_x"].value == 10.0
    assert ctx["session"] == {"host": "h1"}


def test_load_jsonl_rejects_invalid_line(tmp_path: Path) -> None:
    # A clean CLI error naming the file and line, not a ValueError traceback.
    p = tmp_path / "a.jsonl"
    p.write_text(json.dumps(_row("b", 1.0)) + "\nnot json\n")
    with pytest.raises(SystemExit, match="a.jsonl:2: invalid JSON"):
        _load(p, "real_time")


@pytest.mark.parametrize("line", ["[1, 2]", '{"context": {}}', '{"name": "no benchmark"}'])
def test_load_jsonl_rejects_non_row_lines(tmp_path: Path, line: str) -> None:
    # Pure NDJSON: every line is a row with `name` and `benchmark`.
    p = tmp_path / "a.jsonl"
    p.write_text(json.dumps(_row("b", 1.0)) + "\n" + line + "\n")
    with pytest.raises(SystemExit, match="a.jsonl:2: expected a mew result row"):
        _load(p, "real_time")


def test_load_rejects_json_with_non_object_rows(tmp_path: Path) -> None:
    p = tmp_path / "a.json"
    p.write_text('{"context": {}, "benchmarks": [1]}')
    with pytest.raises(SystemExit, match=r"benchmarks\[0\]: expected a mew result row"):
        _load(p, "real_time")


def test_load_rejects_malformed_json(tmp_path: Path) -> None:
    p = tmp_path / "a.json"
    p.write_text("not json")
    with pytest.raises(SystemExit, match="a.json: invalid JSON"):
        _load(p, "real_time")


def test_load_rejects_json_without_benchmarks_array(tmp_path: Path) -> None:
    p = tmp_path / "a.json"
    p.write_text("[1, 2]")  # valid JSON, wrong shape
    with pytest.raises(SystemExit, match="missing 'benchmarks' array"):
        _load(p, "real_time")


def test_compare_missing_file_is_clean_error(tmp_path: Path) -> None:
    present = tmp_path / "a.json"
    _write_json(present, [_row("bench_x", 10.0)])
    with pytest.raises(SystemExit, match="result file not found"):
        compare([present, tmp_path / "missing.json"], console=Console())


def test_compare_jsonl_files(tmp_path: Path) -> None:
    other, base = _write_pair(
        tmp_path, other=[_row("bench_x", 50.0)], base=[_row("bench_x", 100.0)], suffix=".jsonl"
    )
    console = Console(width=200)
    assert compare([other, base], console=console) == 0
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

    console = Console(width=200)
    assert compare([other, base], console=Console()) == 1  # no overlap by name
    code = compare([other, base], key="func", console=console)
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
    assert _split_selector(f"{p}@latest") == (p, "latest")
    assert _split_selector(str(p)) == (p, None)


def test_compare_two_sessions_of_one_file(tmp_path: Path) -> None:
    p = _two_session_file(tmp_path)
    console = Console(width=200)
    code = compare([Path(f"{p}@after"), Path(f"{p}@before")], console=console)
    assert code == 0
    out = console.export_text()
    # Selector-aware labels keep the two columns distinct.
    assert "results.json@before" in out or "results@before" in out
    assert "-20.00%" in out  # 100 -> 80
    # Session shows in the provenance line.
    assert "session=before" in out
    assert "session=after" in out


def test_same_second_runs_stay_distinct_by_session_id(tmp_path: Path) -> None:
    # Two runs in one wall-clock second on one host: the id keeps them apart.
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
    assert _load(p, "real_time", selector="before")[0]["b"].value == 10.0
    assert _load(p, "real_time", selector="after")[0]["b"].value == 20.0


def test_load_with_selector(tmp_path: Path) -> None:
    p = _two_session_file(tmp_path)
    samples, ctx = _load(p, "real_time", selector="before")
    assert samples["b"].value == 100.0
    assert ctx["session"]["tag"] == "before"
    latest, ctx = _load(p, "real_time", selector="latest")
    assert latest["b"].value == 80.0
    assert ctx["session"]["tag"] == "after"


def test_tag_selector_pools_runs_sharing_the_tag(tmp_path: Path) -> None:
    """Repeated runs under one tag reduce together, so an interleaved A/B loop
    keeps every repetition. Without a selector only the newest run counts."""
    p = tmp_path / "r.json"
    _write_json(
        p,
        [
            _row("b", 10.0, date="2026-01-01T00:00:00", session_id="aaaa", session_tag="ab"),
            _row("b", 20.0, date="2026-02-01T00:00:00", session_id="bbbb", session_tag="ab"),
            _row("b", 99.0, date="2026-03-01T00:00:00", session_id="cccc", session_tag="other"),
        ],
    )
    pooled, ctx = _load(p, "real_time", selector="ab")
    assert pooled["b"].values == (10.0, 20.0)
    assert pooled["b"].value == 15.0
    assert ctx["session"]["id"] == "bbbb"  # provenance from the newest pooled run
    newest, _ = _load(p, "real_time")
    assert newest["b"].value == 99.0


def test_unknown_selector_lists_the_tags(tmp_path: Path) -> None:
    p = _two_session_file(tmp_path)
    with pytest.raises(
        SystemExit, match=r"no session tagged 'nope' \(tags in file: after, before\)"
    ):
        _load(p, "real_time", selector="nope")
    with pytest.raises(SystemExit, match="empty session selector"):
        _load(p, "real_time", selector="")


def test_unknown_selector_elides_a_long_tag_list(tmp_path: Path) -> None:
    p = tmp_path / "r.json"
    _write_json(p, [_row("b", 1.0, session_id=f"s{i}", session_tag=f"t{i:02d}") for i in range(9)])
    with pytest.raises(SystemExit, match=r"t00, t01, t02, t03, t04 \(\+4 more\)"):
        _load(p, "real_time", selector="nope")


def test_session_summaries_newest_first(tmp_path: Path) -> None:
    from mew.compare import session_summaries

    p = tmp_path / "r.json"
    new = dict(date="2026-02-01T00:00:00", session_id="new")
    _write_json(
        p,
        [
            _row("b", 1.0, date="2026-01-01T00:00:00", session_id="old", host_name="h"),
            _row("b/case:0", 1.0, benchmark="b[n=1]", session_tag="t", **new),
            _row("b/case:0", 2.0, benchmark="b[n=1]", session_tag="t", **new),
            _row("b/case:0_mean", 1.5, benchmark="b[n=1]", aggregate_name="mean", **new),
        ],
    )
    newest, oldest = session_summaries(p)
    assert (newest.id, newest.tag, newest.benchmarks, newest.rows) == ("new", "t", 1, 3)
    assert (oldest.id, oldest.host, oldest.tag, oldest.benchmarks, oldest.rows) == (
        "old",
        "h",
        None,
        1,
        1,
    )


def test_compare_parses_a_shared_file_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import mew.compare as compare_mod

    p = _two_session_file(tmp_path)
    calls: list[Path] = []
    real = compare_mod._read_rows

    def counting(path: Path):
        calls.append(path)
        return real(path)

    monkeypatch.setattr(compare_mod, "_read_rows", counting)
    assert compare([Path(f"{p}@after"), Path(f"{p}@before")], console=Console(width=200)) == 0
    assert calls == [p]


def test_load_without_measurements_errors(tmp_path: Path) -> None:
    p = tmp_path / "r.json"
    _write_json(p, [_row("b", 0.0, skipped=True)])
    with pytest.raises(SystemExit, match="no measurements"):
        _load(p, "real_time")


def test_compare_prints_context_and_warns_on_skew(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    other, base = _write_pair(
        tmp_path,
        other=[_row("bench_x", 80.0)],
        base=[_row("bench_x", 100.0)],
        other_context={
            "session": {"host": "h2"},
            "context": {"num_cpus": 4, "engine": "duckdb 1.5.3"},
        },
        base_context={
            "session": {"host": "h1"},
            "context": {"num_cpus": 8, "engine": "ducky 0.1"},
        },
    )
    console = Console(width=200)
    assert compare([other, base], console=console) == 0
    out = console.export_text()
    # Per-file provenance headers.
    assert "host=h1" in out
    assert "host=h2" in out
    # Differing custom keys annotate the column labels.
    assert "engine=ducky 0.1" in out
    assert "engine=duckdb 1.5.3" in out
    err = capsys.readouterr().err
    assert "host" in err
    assert "num_cpus" in err


def test_compare_no_skew_warning_when_contexts_match(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ctx = {"host_name": "h1", "num_cpus": 8}
    other, base = _write_pair(
        tmp_path,
        other=[_row("bench_x", 80.0)],
        base=[_row("bench_x", 100.0)],
        other_context=ctx,
        base_context=ctx,
    )
    assert compare([other, base], console=Console(width=200)) == 0
    assert "differ in" not in capsys.readouterr().err


def test_load_renders_case_rows_by_label(tmp_path: Path) -> None:
    p = tmp_path / "a.json"
    _write_json(
        p,
        [
            _row(
                "bench.py::bench_udf/case:0/min_time:0.200",
                10.0,
                benchmark="bench.py::bench_udf[n=100]",
            ),
            _row(
                "bench.py::bench_udf/case:1/min_time:0.200",
                20.0,
                benchmark="bench.py::bench_udf[n=10000]",
            ),
        ],
    )
    samples, _ = _load(p, "real_time")
    assert set(samples) == {"bench.py::bench_udf[n=100]", "bench.py::bench_udf[n=10000]"}


def test_compare_aligns_files_run_with_different_min_time(tmp_path: Path) -> None:
    other, base = _write_pair(
        tmp_path,
        other=[_row("a.py::bench_x/case:0/min_time:0.500", 80.0, benchmark="a.py::bench_x[n=10]")],
        base=[_row("a.py::bench_x/case:0/min_time:0.200", 100.0, benchmark="a.py::bench_x[n=10]")],
    )
    console = Console(width=200)
    assert compare([other, base], console=console) == 0
    out = console.export_text()
    assert "a.py::bench_x[n=10]" in out
    assert "-20.00%" in out


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
    other, base = _write_pair(
        tmp_path,
        other=[_mem_row("bench_x", 1.0, peak=1 << 21, allocs=50)],
        base=[_mem_row("bench_x", 1.0, peak=1 << 20, allocs=100)],
    )

    console = Console(width=200)
    code = compare([other, base], metric="memory.peak_bytes", console=console)
    assert code == 0
    out = console.export_text()
    assert "1.0 MB" in out  # byte-formatted baseline
    assert "+100.00%" in out
    # Memory isn't a speedup: the ratio column is neutrally labelled.
    assert "ratio" in out
    assert "speedup" not in out

    console = Console(width=200)
    code = compare([other, base], metric="memory.allocations_per_iteration", console=console)
    assert code == 0
    assert "-50.00%" in console.export_text()


def test_compare_time_metric_keeps_speedup_header(tmp_path: Path) -> None:
    other, base = _write_pair(
        tmp_path, other=[_row("bench_x", 80.0)], base=[_row("bench_x", 100.0)]
    )
    console = Console(width=200)
    compare([other, base], metric="real_time", console=console)
    out = console.export_text()
    assert "speedup" in out
    assert "ratio" not in out


def test_compare_allocations_per_iteration_is_speed_independent(tmp_path: Path) -> None:
    # The whole point of item 0: two engines whose raw total_allocations differ
    # only because they ran a different iteration count compare *equal* per-iter.
    # Same per-call allocations (10), captured over different iteration counts.
    other, base = _write_pair(
        tmp_path,
        other=[_mem_row("bench_x", 1.0, peak=1 << 20, allocs=500, iterations=50)],
        base=[_mem_row("bench_x", 1.0, peak=1 << 20, allocs=1000, iterations=100)],
    )

    console = Console(width=200)
    code = compare([other, base], metric="memory.allocations_per_iteration", console=console)
    assert code == 0
    out = console.export_text()
    assert "10.0" in out  # baseline per-iteration count, fractional format
    assert "+0.00%" in out  # identical per-call work despite 2× raw allocs


def test_compare_memory_metric_without_data_hints_profile_flag(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    other, base = _write_pair(tmp_path, other=[_row("bench_x", 1.0)], base=[_row("bench_x", 1.0)])
    code = compare([other, base], metric="memory.peak_bytes", console=Console())
    assert code == 1
    assert "--profile-memory" in capsys.readouterr().err


def test_compare_marks_high_cv_rows(tmp_path: Path) -> None:
    # Baseline reps scatter wildly (CV >> 25%); other is steady.
    other, base = _write_pair(
        tmp_path,
        other=[_row("b", 99.0), _row("b", 100.0), _row("b", 101.0)],
        base=[_row("b", 40.0), _row("b", 100.0), _row("b", 160.0)],
    )
    console = Console(width=200)
    assert compare([other, base], console=console) == 0
    out = console.export_text()
    assert "(!)" in out
    assert "±60%" in out  # stdev([40,100,160])/median = 60/100


def test_compare_no_cv_marker_on_steady_rows(tmp_path: Path) -> None:
    other, base = _write_pair(
        tmp_path,
        other=[_row("b", 49.0), _row("b", 50.0), _row("b", 51.0)],
        base=[_row("b", 99.0), _row("b", 100.0), _row("b", 101.0)],
    )
    console = Console(width=200)
    assert compare([other, base], console=console) == 0
    assert "(!)" not in console.export_text()


def test_compare_no_significance_marker_on_insignificant_delta(tmp_path: Path) -> None:
    # Both sides scatter over the same range; the ~2% delta shouldn't read as real.
    other, base = _write_pair(
        tmp_path,
        other=[_row("b", 99.0), _row("b", 100.0), _row("b", 101.0), _row("b", 100.0)],
        base=[_row("b", 101.0), _row("b", 99.0), _row("b", 103.0), _row("b", 98.0)],
    )
    console = Console(width=200)
    assert compare([other, base], console=console) == 0
    assert "(signif.)" not in console.export_text()


def test_compare_marks_significant_delta_on_clear_shift(tmp_path: Path) -> None:
    # n=3 per side tops out at p~0.08 (exact Mann-Whitney floor is 0.1) even at
    # total separation, so this needs enough reps for total separation to clear
    # the 0.05 bar.
    other, base = _write_pair(
        tmp_path,
        other=[_row("b", v) for v in (9.0, 10.0, 11.0, 9.5, 10.5)],
        base=[_row("b", v) for v in (99.0, 100.0, 101.0, 99.5, 100.5)],
    )
    console = Console(width=200)
    assert compare([other, base], console=console) == 0
    assert "(signif.)" in console.export_text()


def test_compare_no_significance_marker_without_repetitions(tmp_path: Path) -> None:
    other, base = _write_pair(tmp_path, other=[_row("b", 10.0)], base=[_row("b", 100.0)])
    console = Console(width=200)
    assert compare([other, base], console=console) == 0
    assert "(signif.)" not in console.export_text()


def test_load_selector_drops_aggregate_rows(tmp_path: Path) -> None:
    """A ``@selector`` load must filter GB aggregate rows like the default load.

    With repetitions > 1 the file carries mean/stddev/cv rows; mixing them into
    the recomputed statistics drags the median toward the tiny cv/stddev values.
    """
    common = dict(date="2026-01-01T00:00:00", session_id="0197-aaaa", session_tag="before")
    rows = [
        _row("b", 100.0, repetition_index=0, **common),
        _row("b", 110.0, repetition_index=1, **common),
        _row("b", 120.0, repetition_index=2, **common),
        _row("b", 110.0, aggregate_name="mean", **common),
        _row("b", 10.0, aggregate_name="stddev", **common),
        _row("b", 0.09, aggregate_name="cv", **common),
    ]
    p = tmp_path / "r.jsonl"
    p.write_text("".join(json.dumps(r) + "\n" for r in rows))

    selected, _ = _load(p, "real_time", selector="before")
    full, _ = _load(p, "real_time")
    assert selected["b"].value == 110.0
    assert selected["b"].value == full["b"].value


def test_load_excludes_skipped_rows(tmp_path: Path) -> None:
    # Skipped rows carry no real timing; including them would drag the median
    # toward 0. A benchmark with only skipped rows must not produce a sample.
    p = tmp_path / "a.json"
    _write_json(
        p,
        [
            _row("b", 100.0),
            _row("b", 120.0),
            _row("b", 0.0, skipped=True),
            _row("only_skipped", 0.0, skipped=True),
        ],
    )
    samples, _ = _load(p, "real_time")
    assert samples["b"].value == 110.0  # median of the two real rows
    assert "only_skipped" not in samples


def test_compare_zero_baseline_shows_infinite_delta(tmp_path: Path) -> None:
    # A zero baseline against a nonzero contender must not read as +0.00%.
    other, base = _write_pair(tmp_path, other=[_row("b", 50.0)], base=[_row("b", 0.0)])
    console = Console(width=200)
    compare([other, base], console=console)
    assert "+∞%" in console.export_text()


def test_fmt_delta_colors_by_improvement_direction() -> None:
    from mew.compare import _fmt_delta

    assert _fmt_delta(0.2) == ("+20.00%", "red")  # slower time: bad
    assert _fmt_delta(-0.2) == ("-20.00%", "green")
    # More iterations is an improvement, fewer a regression.
    assert _fmt_delta(0.2, higher_is_better=True)[1] == "green"
    assert _fmt_delta(-0.2, higher_is_better=True)[1] == "red"


def test_fmt_delta_no_change_has_no_color_style() -> None:
    from mew.compare import _fmt_delta

    # A tie must format as neutral +0.00%, styled with neither red nor green.
    assert _fmt_delta(0.0) == ("+0.00%", "")
    assert _fmt_delta(0.0, higher_is_better=True) == ("+0.00%", "")


def test_compare_zero_baseline_and_zero_contender_is_no_change(tmp_path: Path) -> None:
    # Both sides zero (e.g. an allocations-per-iteration counter that's zero in
    # both files) must format as a plain +0.00% tie, not `+∞%` or a
    # ZeroDivisionError — only a *nonzero* contender against a zero baseline is
    # the "infinite improvement" case.
    other, base = _write_pair(tmp_path, other=[_row("b", 0.0)], base=[_row("b", 0.0)])
    console = Console(width=200)
    code = compare([other, base], console=console)
    assert code == 0
    out = console.export_text()
    assert "+0.00%" in out
    assert "∞" not in out


def test_compare_iterations_speedup_direction(tmp_path: Path) -> None:
    # +20% iterations is a ×1.2 speedup, not ×0.83.
    other, base = _write_pair(
        tmp_path,
        other=[_row("b", 1.0, iterations=1200)],
        base=[_row("b", 1.0, iterations=1000)],
    )
    console = Console(width=200)
    compare([other, base], metric="iterations", console=console)
    assert "×1.200" in console.export_text()


def test_compare_warns_on_time_unit_skew(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    other, base = _write_pair(
        tmp_path,
        other=[_row("b", 0.1, time_unit="us")],
        base=[_row("b", 100.0)],  # ns
    )
    compare([other, base], console=Console(width=200))
    err = capsys.readouterr().err
    assert "different time units" in err


def test_compare_custom_statistic_error_is_surfaced(tmp_path: Path) -> None:
    # A reducer that raises must fail loudly, not silently drop every benchmark
    # and report "no overlapping benchmarks".
    import statistics as _stats

    other, base = _write_pair(tmp_path, other=[_row("b", 120.0)], base=[_row("b", 100.0)])
    # stdev needs two values; these files have one repetition each.
    with pytest.raises(SystemExit, match="--statistic failed on 'b'"):
        compare([other, base], statistic=_stats.stdev, console=Console(width=200))


def _two_session_jsonl(tmp_path: Path) -> Path:
    """A self-contained JSONL file holding 'before' (100) and 'after' (80) sessions."""
    p = tmp_path / "results.jsonl"
    rows = [
        _row("b", 100.0, date="2026-01-01T00:00:00", session_id="0197aaaa11", session_tag="before"),
        _row("b", 80.0, date="2026-02-01T00:00:00", session_id="0197bbbb22", session_tag="after"),
    ]
    p.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return p


def test_compare_two_sessions_of_one_jsonl_file(tmp_path: Path) -> None:
    p = _two_session_jsonl(tmp_path)
    console = Console(width=200)
    assert compare([Path(f"{p}@after"), Path(f"{p}@before")], console=console) == 0
    out = console.export_text()
    assert "-20.00%" in out  # 100 -> 80: both sessions resolved from row-level identity
    assert "session=before" in out and "session=after" in out


def test_compare_jsonl_gz_roundtrip(tmp_path: Path) -> None:
    import gzip

    def write(path: Path, rows: list[dict]) -> None:
        with gzip.open(path, "wt") as fh:
            fh.writelines(json.dumps(r) + "\n" for r in rows)

    base = tmp_path / "base.jsonl.gz"
    other = tmp_path / "other.jsonl.gz"
    write(base, [_row("bench_x", 100.0)])
    write(other, [_row("bench_x", 50.0)])
    console = Console(width=200)
    code = compare([other, base], console=console)
    assert code == 0
    out = console.export_text()
    assert "-50.00%" in out
    assert "×2.000" in out


def test_read_results_returns_rows_as_stored(tmp_path: Path) -> None:
    """The raw view: nothing filtered, rows self-describing whatever the source."""
    p = tmp_path / "r.json"
    _write_json(
        p,
        [
            _row("b", 10.0, session_id="s1", host_name="h", custom={"engine": "x"}),
            _row("b", 0.0, aggregate_name="mean", session_id="s1", host_name="h"),
        ],
    )
    rows = read_results(p)
    assert len(rows) == 2, "aggregate rows are not filtered here"
    assert rows[0]["session"]["host"] == "h"
    assert rows[0]["context"]["engine"] == "x"


@pytest.mark.parametrize("family", [False, True])
def test_thread_counts_remain_distinct_samples(tmp_path: Path, family: bool) -> None:
    path = tmp_path / "threads.json"
    prefix = "b/case:0" if family else "b"
    base = "b[size=10]" if family else "b"
    _write_json(
        path,
        [
            _row(
                f"{prefix}/iterations:10/threads:{n}",
                value,
                label="size=10" if family else "",
                threads=n,
                benchmark=f"{base}/threads:{n}",
            )
            for n, value in [(1, 100), (2, 200), (4, 400)]
        ],
    )
    samples, _ = _load(path, "real_time")
    assert {name: sample.value for name, sample in samples.items()} == {
        f"{base}/threads:1": 100,
        f"{base}/threads:2": 200,
        f"{base}/threads:4": 400,
    }
    assert all(sample.stddev is None for sample in samples.values())


@pytest.mark.parametrize("metric", ["real_time", "cpu_time"])
@pytest.mark.parametrize("reverse", [False, True])
def test_grouped_sessions_normalize_units_before_reduction(
    tmp_path: Path, metric: str, reverse: bool
) -> None:
    path = tmp_path / "units.jsonl"
    rows = [
        _row("b", 1000, time_unit="ns", session_tag="same", date="2026-01-01"),
        _row("b", 1, time_unit="us", session_tag="same", date="2026-01-02"),
    ]
    if reverse:
        rows.reverse()
    _write_jsonl(path, rows)
    samples, _ = _load(path, metric, selector="same")
    sample = samples["b"]
    # Pooled rows are ordered by session date, so the oldest run's unit wins
    # regardless of file order.
    assert sample.value == 1000
    assert sample.stddev == 0
    assert sample.values == (1000, 1000)
    assert sample.time_unit == "ns"


@pytest.mark.parametrize("show_stddev", [False, True])
def test_history_missing_a_benchmark_cannot_hide_regression(
    tmp_path: Path, capsys, show_stddev: bool
) -> None:
    from mew.regressions import RegressionConfig

    head, history, baseline = [tmp_path / f"{name}.json" for name in ("head", "history", "base")]
    _write_json(head, [_row("slow", 200), _row("ok", 100)])
    _write_json(baseline, [_row("slow", 100), _row("ok", 100)])
    _write_json(history, [_row("ok", 100)])
    console = Console(width=200)
    assert (
        compare(
            [head, history, baseline],
            regressions=RegressionConfig(5),
            show_stddev=show_stddev,
            console=console,
        )
        == 2
    )
    assert "slow" in console.export_text()
    assert "+100.00%" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("metric", "base_value", "other_value", "base_unit", "other_unit", "delta", "ratio"),
    [
        ("real_time", 1000, 1, "ns", "us", 0, 1),
        ("cpu_time", 1, 2000, "us", "ns", 1, 0.5),
        ("iterations", 100, 200, "ns", "us", 1, 2),
        ("memory.peak_bytes", 100, 200, "ns", "us", 1, 0.5),
        ("real_time", 0, 1, "ns", "ns", float("inf"), 0),
        ("real_time", 1, 0, "ns", "ns", -1, float("inf")),
    ],
)
def test_comparison_math_without_rendering(
    metric, base_value, other_value, base_unit, other_unit, delta, ratio
):
    from mew.compare import Sample, _compare_samples

    base = Sample("b", base_value, None, base_unit)
    other = Sample("b", other_value, None, other_unit)
    result = _compare_samples(base, other, metric)
    assert result.delta == delta
    assert result.speedup == ratio
    assert result.p_value is None

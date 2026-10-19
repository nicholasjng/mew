"""Tests for `mew.compare`."""

from __future__ import annotations

import gzip
import json
import math
import re
import statistics
from pathlib import Path

import pytest
from _helpers import (
    Console,
    row as _row,
    write_json as _write_json,
    write_jsonl as _write_jsonl,
    write_pair as _write_pair,
)

from mew._results import _load, _split_selector
from mew._statistics import Statistic
from mew.compare import (
    Sample,
    _compare_samples,
    _fmt_delta,
    compare,
    read_results,
    session_summaries,
)
from mew.regressions import RegressionConfig


def test_compare_custom_statistic_end_to_end(tmp_path: Path) -> None:
    # Both files: a tight cluster plus one outlier. median ignores it; max picks it.
    other, base = _write_pair(
        tmp_path,
        other=[_row("b", 1.0), _row("b", 2.0), _row("b", 51.0)],
        base=[_row("b", 1.0), _row("b", 2.0), _row("b", 99.0)],
    )

    console = Console(width=200)
    code = compare([other, base], statistic=max, console=console)
    assert code == 0
    out = console.export_text()
    assert "99.00 ns" in out
    assert "51.00 ns" in out
    assert "-48.48%" in out


def test_load_skips_aggregate_and_skipped_rows(tmp_path: Path) -> None:
    """Older files' Google Benchmark aggregate rows and skipped rows are not
    repetitions: they must not skew the median, and a benchmark with only
    skipped rows has no sample."""
    p = tmp_path / "a.json"
    _write_json(
        p,
        [
            _row("b", 5.0),
            _row("b", 7.0),
            _row("b", 999.0, aggregate_name="mean"),
            _row("b", 0.0, skipped=True),
            _row("only_skipped", 0.0, skipped=True),
        ],
    )
    samples, _ = _load(p, "real_time")
    assert samples.keys() == {"b"}
    assert samples["b"].values == (5.0, 7.0)


def test_compare_speedup_signs(tmp_path: Path) -> None:
    other, base = _write_pair(
        tmp_path,
        other=[_row("bench_x", 80.0), _row("bench_y", 75.0)],
        base=[_row("bench_x", 100.0), _row("bench_y", 50.0)],
    )

    console = Console(width=200)
    code = compare([other, base], console=console)
    assert code == 0
    out = console.export_text()
    assert "-20.00%" in out
    assert "×1.250" in out
    assert "+50.00%" in out
    assert "×0.667" in out
    # Both absolute timings are shown, not just the baseline's.
    assert "100.00 ns" in out
    assert "80.00 ns" in out
    assert "75.00 ns" in out
    # A time metric's ratio is a speedup (memory's is a neutral "ratio").
    assert "speedup" in out and "ratio" not in out


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
    assert "bench_x" in out
    assert "bench_only_in_base" not in out


def test_compare_empty_overlap(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    other, base = _write_pair(tmp_path, other=[_row("b", 1.0)], base=[_row("a", 1.0)])
    code = compare([other, base], console=Console())
    assert code == 1
    assert "no overlapping benchmarks" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("pattern", "literal", "shown"),
    [("alpha|gamma", False, ["alpha", "gamma"]), ("beta[n=1]", True, ["beta[n=1]"])],
)
def test_compare_pattern_is_a_regex_unless_literal(
    tmp_path: Path, pattern: str, literal: bool, shown: list[str]
) -> None:
    names = ["alpha", "beta[n=1]", "gamma"]
    other, base = _write_pair(
        tmp_path, other=[_row(n, 5.0) for n in names], base=[_row(n, 10.0) for n in names]
    )
    console = Console(width=200)
    assert compare([other, base], pattern=pattern, literal=literal, console=console) == 0
    out = console.export_text()
    assert [n for n in names if n in out] == shown


@pytest.mark.parametrize(
    ("n_files", "kwargs", "match"),
    [
        (1, {}, "at least two"),
        (2, {"metric": "bogus"}, "unknown metric"),
        (2, {"key": "bogus"}, "unknown key"),
        (2, {"pattern": "foo("}, "invalid benchmark filter pattern"),
    ],
)
def test_compare_rejects_bad_arguments(
    tmp_path: Path, n_files: int, kwargs: dict, match: str
) -> None:
    # Checked before any file is read, so the files need not exist.
    files = [tmp_path / f"{i}.json" for i in range(n_files)]
    with pytest.raises(ValueError, match=match):
        compare(files, **kwargs)


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


@pytest.mark.parametrize(
    ("metric", "stddevs"),
    [
        ("real_time", ["1.00 ns", "2.00 ns"]),
        # 0.2.0 printed the peak_bytes stddev unscaled, as "1048576.00".
        ("memory.peak_bytes", ["1.0 MB", "2.0 MB"]),
    ],
)
def test_compare_stddev_column_uses_the_value_unit(
    tmp_path: Path, metric: str, stddevs: list[str]
) -> None:
    mib = 1 << 20
    other, base = _write_pair(
        tmp_path,
        other=[_mem_row("b", v, peak=v * mib, allocs=1) for v in (2, 4, 6)],
        base=[_mem_row("b", v, peak=v * mib, allocs=1) for v in (4, 5, 6)],
    )
    console = Console(width=200)
    assert compare([other, base], metric=metric, show_stddev=True, console=console) == 0
    # The row's cells: name, base, ± stddev, other, Δ%, ratio, ± stddev.
    cells = [c.strip() for c in console.export_text().splitlines()[-1].split("│")]
    assert [cells[2], cells[-1]] == stddevs


_ROW_LINE = json.dumps(_row("b", 1.0))


@pytest.mark.parametrize(
    ("filename", "content", "match"),
    [
        ("a.json", None, "result file not found"),
        ("a.txt", "", "unsupported result file"),
        ("a.json", "not json", "a.json: invalid JSON"),
        ("a.json", "[1, 2]", "missing 'benchmarks' array"),  # valid JSON, wrong shape
        ("a.json", '{"benchmarks": [1]}', r"benchmarks\[0\]: expected a mew result row"),
        ("a.jsonl", _ROW_LINE + "\nnot json\n", "a.jsonl:2: invalid JSON"),
        # Pure NDJSON: every line is a row with `name` and `benchmark`.
        ("a.jsonl", _ROW_LINE + '\n{"name": "x"}\n', "a.jsonl:2: expected a mew result row"),
        # Cut off, as by a full disk or an interrupted copy.
        # mtime=0: a timestamp in the header would vary the test ID between xdist workers.
        ("a.jsonl.gz", gzip.compress(_ROW_LINE.encode(), mtime=0)[:-8], "cannot read result file"),
    ],
)
def test_load_rejects_unreadable_files(
    tmp_path: Path, filename: str, content: str | bytes | None, match: str
) -> None:
    # A one-line error naming the file (and line), which the CLI prints.
    p = tmp_path / filename
    if isinstance(content, bytes):
        p.write_bytes(content)
    elif content is not None:
        p.write_text(content)
    with pytest.raises(FileNotFoundError if content is None else ValueError, match=match):
        _load(p, "real_time")


def test_load_key_func_rejects_collisions(tmp_path: Path) -> None:
    p = tmp_path / "a.json"
    _write_json(p, [_row("a.py::bench_x", 1.0), _row("b.py::bench_x", 2.0)])
    with pytest.raises(ValueError, match="maps both"):
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


@pytest.mark.parametrize(
    ("raw", "path", "selector"),
    [
        ("results.json", "results.json", None),
        ("results.json@before", "results.json", "before"),
        ("v@2/results.JSONL.GZ@before", "v@2/results.JSONL.GZ", "before"),
        ("results.jsonl@", "results.jsonl", ""),  # loading rejects the empty selector
        # Only an `@` after a result-file extension splits, whether or not the
        # file exists.
        ("weird@name.json", "weird@name.json", None),
        ("results@before", "results@before", None),
    ],
)
def test_split_selector(raw: str, path: str, selector: str | None) -> None:
    assert _split_selector(raw) == (Path(path), selector)


def test_compare_two_sessions_of_one_file(tmp_path: Path) -> None:
    p = tmp_path / "results.json"
    _write_json(
        p,
        [
            _row("b", 100.0, date="2026-01-01", session_id="0197aaaa11", session_tag="before"),
            _row("b", 80.0, date="2026-02-01", session_id="0197bbbb22", session_tag="after"),
        ],
    )
    console = Console(width=200)
    assert compare([Path(f"{p}@after"), Path(f"{p}@before")], console=console) == 0
    out = console.export_text()
    # Selector-aware labels keep the two columns distinct.
    assert "@before" in out and "@after" in out
    assert "-20.00%" in out  # 100 -> 80
    assert "session=before" in out and "session=after" in out


@pytest.mark.parametrize("suffix", [".json", ".jsonl", ".jsonl.gz"])
def test_compare_reads_every_result_format(tmp_path: Path, suffix: str) -> None:
    other, base = _write_pair(
        tmp_path, other=[_row("bench_x", 50.0)], base=[_row("bench_x", 100.0)], suffix=suffix
    )
    console = Console(width=200)
    assert compare([other, base], console=console) == 0
    out = console.export_text()
    assert "-50.00%" in out and "×2.000" in out


def test_load_gzip_jsonl_with_uppercase_suffix(tmp_path: Path) -> None:
    # The suffix check is case-insensitive, so the gzip check must be too.
    p = tmp_path / "a.JSONL.GZ"
    with gzip.open(p, "wt") as fh:
        fh.write(json.dumps(_row("bench_x", 10.0)) + "\n")
    samples, _ = _load(p, "real_time")
    assert samples["bench_x"].value == 10.0


def test_same_second_runs_stay_distinct_and_order_by_session_id(tmp_path: Path) -> None:
    # Two runs in one wall-clock second: the time-ordered id keeps them apart
    # and orders them, though their hosts sort the other way.
    p = tmp_path / "agg.json"
    when = "2026-06-12T10:00:00"
    _write_json(
        p,
        [
            _row(
                "b", 10.0, date=when, host_name="h2", session_id="0197-aaaa", session_tag="before"
            ),
            _row("b", 20.0, date=when, host_name="h1", session_id="0197-bbbb", session_tag="after"),
        ],
    )
    assert _load(p, "real_time", selector="before")[0]["b"].value == 10.0
    assert _load(p, "real_time")[0]["b"].value == 20.0
    assert [s.id for s in session_summaries(p)] == ["0197-bbbb", "0197-aaaa"]


def test_tag_selector_pools_runs_sharing_the_tag(tmp_path: Path) -> None:
    """Repeated runs under one tag reduce together, so an interleaved A/B loop
    keeps every repetition. Without a selector, or with `@latest`, only the
    newest run counts."""
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
    for selector in (None, "latest"):
        newest, ctx = _load(p, "real_time", selector=selector)
        assert newest["b"].value == 99.0
        assert ctx["session"]["tag"] == "other"


def test_unknown_selector_lists_the_tags(tmp_path: Path) -> None:
    p = tmp_path / "r.json"
    _write_json(p, [_row("b", 1.0, session_id=f"s{i}", session_tag=f"t{i}") for i in range(7)])
    with pytest.raises(
        ValueError,
        match=r"no session tagged 'nope' \(tags in file: t0, t1, t2, t3, t4 \(\+2 more\)",
    ):
        _load(p, "real_time", selector="nope")
    with pytest.raises(ValueError, match="empty session selector"):
        _load(p, "real_time", selector="")


def test_session_summaries_newest_first(tmp_path: Path) -> None:
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
    # `benchmarks` counts measured benchmarks; `rows` counts every stored row.
    assert (newest.id, newest.tag, newest.benchmarks, newest.rows) == ("new", "t", 1, 3)
    assert (oldest.id, oldest.host, oldest.tag, oldest.benchmarks, oldest.rows) == (
        "old",
        "h",
        None,
        1,
        1,
    )


def test_load_without_measurements_errors(tmp_path: Path) -> None:
    p = tmp_path / "r.json"
    _write_json(p, [_row("b", 0.0, skipped=True)])
    with pytest.raises(ValueError, match="no measurements"):
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
            "context": {"cpu_model": "Apple M2", "num_cpus": 4, "engine": "duckdb 1.5.3"},
        },
        base_context={
            "session": {"host": "h1"},
            "context": {"cpu_model": "Apple M1", "num_cpus": 8, "engine": "ducky 0.1"},
        },
    )
    console = Console(width=200)
    assert compare([other, base], console=console) == 0
    out = console.export_text()
    assert "host=h1" in out
    assert "host=h2" in out
    # Differing custom keys annotate the column labels.
    assert "engine=ducky 0.1" in out
    assert "engine=duckdb 1.5.3" in out
    err = capsys.readouterr().err
    assert "host" in err
    assert "differ in cpu_model (base: Apple M1, other: Apple M2)" in err
    assert "num_cpus" in err


def test_compare_labels_columns_by_interpreter(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A suite run on two interpreters of one machine compares by version,
    without a skew warning."""
    python = {"implementation": "cpython", "gil_enabled": True}
    machine = {"num_cpus": 8, "cpu_model": "Apple M1"}
    other, base = _write_pair(
        tmp_path,
        other=[_row("bench_x", 80.0)],
        base=[_row("bench_x", 100.0)],
        other_context={
            "session": {"host": "h1"},
            "context": {**machine, "python": {**python, "version": "3.15.0"}},
        },
        base_context={
            "session": {"host": "h1"},
            "context": {**machine, "python": {**python, "version": "3.14.3"}},
        },
    )
    console = Console(width=200)
    assert compare([other, base], console=console) == 0
    out = console.export_text()
    assert "(python.version=3.14.3)" in out
    assert "(python.version=3.15.0)" in out
    assert "host=h1" in out  # the matching machine context was read, and not warned on
    assert "differ in" not in capsys.readouterr().err


def test_rows_group_by_benchmark_not_raw_name(tmp_path: Path) -> None:
    """A row's `benchmark` names its sample: run options in the raw `name`, such
    as min_time, don't split it, and thread counts stay apart (fixed in 0.2.0)."""
    p = tmp_path / "a.json"
    _write_json(
        p,
        [
            _row("b/case:0/min_time:0.500/threads:1", 100.0, benchmark="b[n=1]/threads:1"),
            _row("b/case:0/min_time:0.200/threads:1", 110.0, benchmark="b[n=1]/threads:1"),
            _row("b/case:0/min_time:0.500/threads:2", 200.0, benchmark="b[n=1]/threads:2"),
        ],
    )
    samples, _ = _load(p, "real_time")
    assert {name: s.values for name, s in samples.items()} == {
        "b[n=1]/threads:1": (100.0, 110.0),
        "b[n=1]/threads:2": (200.0,),
    }


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


def test_compare_memory_metric_without_data_hints_profile_flag(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    other, base = _write_pair(tmp_path, other=[_row("bench_x", 1.0)], base=[_row("bench_x", 1.0)])
    code = compare([other, base], metric="memory.peak_bytes", console=Console())
    assert code == 1
    assert "--profile-memory" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("metric", "unit"), [("cpu_time", "us"), ("iterations", None), ("memory.peak_bytes", None)]
)
def test_sample_time_unit_is_set_for_times_only(
    tmp_path: Path, metric: str, unit: str | None
) -> None:
    p = tmp_path / "a.json"
    _write_json(p, [{**_mem_row("b", 1.0, peak=1, allocs=1), "time_unit": "us"}])
    assert _load(p, metric)[0]["b"].time_unit == unit


@pytest.mark.parametrize(
    ("other", "base", "statistic", "markers"),
    [
        # Baseline reps scatter wildly (CV >> 25%): stdev([40,100,160])/mean = 60/100.
        ((99.0, 100.0, 101.0), (40.0, 100.0, 160.0), None, ["±60% (!)"]),
        # The CV divides by the mean whatever the statistic; by min, it would be 150%.
        ((99.0, 100.0, 101.0), (40.0, 100.0, 160.0), min, ["±60% (!)"]),
        # Both sides steady.
        ((49.0, 50.0, 51.0), (99.0, 100.0, 101.0), None, []),
    ],
)
def test_compare_marks_only_high_cv_rows(
    tmp_path: Path,
    other: tuple[float, ...],
    base: tuple[float, ...],
    statistic: Statistic | None,
    markers: list[str],
) -> None:
    other_path, base_path = _write_pair(
        tmp_path, other=[_row("b", v) for v in other], base=[_row("b", v) for v in base]
    )
    console = Console(width=200)
    assert compare([other_path, base_path], statistic=statistic, console=console) == 0
    assert re.findall(r"±\d+% \(!\)", console.export_text()) == markers


@pytest.mark.parametrize(
    ("other", "base", "significant"),
    [
        # Both sides scatter over the same range; the ~2% delta shouldn't read as real.
        ((99.0, 100.0, 101.0, 100.0), (101.0, 99.0, 103.0, 98.0), False),
        # n=3 per side cannot reach p < 0.05 even at total separation; use 5 reps.
        ((9.0, 10.0, 11.0, 9.5, 10.5), (99.0, 100.0, 101.0, 99.5, 100.5), True),
    ],
)
def test_compare_marks_only_significant_deltas(
    tmp_path: Path, other: tuple[float, ...], base: tuple[float, ...], significant: bool
) -> None:
    other_path, base_path = _write_pair(
        tmp_path, other=[_row("b", v) for v in other], base=[_row("b", v) for v in base]
    )
    console = Console(width=200)
    assert compare([other_path, base_path], console=console) == 0
    assert ("(signif.)" in console.export_text()) is significant


def test_fmt_delta_colors_by_improvement_direction() -> None:
    assert _fmt_delta(0.2) == ("+20.00%", "red")  # slower time: bad
    assert _fmt_delta(-0.2) == ("-20.00%", "green")
    # More iterations is an improvement, fewer a regression.
    assert _fmt_delta(0.2, higher_is_better=True)[1] == "green"
    assert _fmt_delta(-0.2, higher_is_better=True)[1] == "red"
    assert _fmt_delta(0.0) == ("+0.00%", "")
    assert _fmt_delta(0.0, higher_is_better=True) == ("+0.00%", "")
    # A zero baseline against a nonzero contender must not read as +0.00%.
    assert _fmt_delta(math.inf) == ("+∞%", "red")


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
    # A failing reducer must fail loudly, not drop every benchmark and report
    # "no overlapping benchmarks".
    def failing(values: list[float]) -> float:
        raise statistics.StatisticsError("needs more data")

    other, base = _write_pair(tmp_path, other=[_row("b", 120.0)], base=[_row("b", 100.0)])
    with pytest.raises(ValueError, match="statistic failed on 'b': needs more data"):
        compare([other, base], statistic=failing, console=Console(width=200))


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


@pytest.mark.parametrize("reverse", [False, True])
def test_grouped_sessions_normalize_units_before_reduction(tmp_path: Path, reverse: bool) -> None:
    path = tmp_path / "units.jsonl"
    rows = [
        _row("b", 1000, time_unit="ns", session_tag="same", date="2026-01-01"),
        _row("b", 1, time_unit="us", session_tag="same", date="2026-01-02"),
    ]
    if reverse:
        rows.reverse()
    _write_jsonl(path, rows)
    sample = _load(path, "real_time", selector="same")[0]["b"]
    # Pooled rows are ordered by session date, so the oldest run's unit wins
    # regardless of file order.
    assert sample.values == (1000, 1000)
    assert sample.time_unit == "ns"


@pytest.mark.parametrize("show_stddev", [False, True])
@pytest.mark.parametrize(("gate", "code"), [(None, 0), (RegressionConfig(5), 3)])
def test_rows_are_the_benchmarks_candidate_and_baseline_share(
    tmp_path: Path, show_stddev: bool, gate: RegressionConfig | None, code: int
) -> None:
    """Gated or not, one row set: a benchmark the history lacks still shows, so it
    can't hide a regression, and one the candidate lacks has nothing to compare."""
    head, history, baseline = [tmp_path / f"{name}.json" for name in ("head", "history", "base")]
    _write_json(head, [_row("slow", 200), _row("ok", 100)])
    _write_json(history, [_row("ok", 100), _row("gone", 100)])
    _write_json(baseline, [_row("slow", 100), _row("ok", 100), _row("gone", 100)])
    console = Console(width=200)
    files = [head, history, baseline]
    assert compare(files, regressions=gate, show_stddev=show_stddev, console=console) == code
    lines = console.export_text().splitlines()
    assert [line.split("│")[0].strip() for line in lines if "│" in line][1:] == ["ok", "slow"]


@pytest.mark.parametrize(
    ("metric", "base_value", "other_value", "base_unit", "other_unit", "delta", "ratio"),
    [
        ("real_time", 1000, 1, "ns", "us", 0, 1),
        ("cpu_time", 1, 2000, "us", "ns", 1, 0.5),
        ("iterations", 100, 200, "ns", "us", 1, 2),
        ("memory.peak_bytes", 100, 200, "ns", "us", 1, 0.5),
        ("real_time", 0, 1, "ns", "ns", float("inf"), 0),
        ("real_time", 1, 0, "ns", "ns", -1, float("inf")),
        # Both zero (e.g. allocation-free bodies) is a tie: 0.2.0 showed ×inf.
        ("memory.peak_bytes", 0, 0, None, None, 0, 1),
    ],
)
def test_comparison_math_without_rendering(
    metric, base_value, other_value, base_unit, other_unit, delta, ratio
):
    base = Sample("b", base_value, None, base_unit)
    other = Sample("b", other_value, None, other_unit)
    result = _compare_samples(base, other, metric)
    assert result.delta == delta
    assert result.speedup == ratio
    assert result.p_value is None

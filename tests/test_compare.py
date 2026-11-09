"""Tests for `mew.compare`."""

from __future__ import annotations

import gzip
import json
import math
import re
from pathlib import Path

import pytest
from _helpers import (
    Console,
    counter_row,
    row as _row,
    write_json as _write_json,
    write_jsonl as _write_jsonl,
    write_pair as _write_pair,
)

from mew._results import Sample, load_column
from mew.compare import (
    _compare_samples,
    _fmt_delta,
    compare,
    read_results,
    run_summaries,
)
from mew.regressions import CounterConfig, CounterRule, RegressionConfig


def test_load_skips_skipped_rows(tmp_path: Path) -> None:
    """Skipped rows are not repetitions: they must not skew the median, and a benchmark with
    only skipped rows has no sample."""
    p = tmp_path / "a.json"
    _write_json(
        p,
        [
            _row("b", 5.0),
            _row("b", 7.0),
            _row("b", 0.0, skipped=True),
            _row("only_skipped", 0.0, skipped=True),
        ],
    )
    samples, _, _ = load_column(p, "real_time")
    assert samples.keys() == {"b"}
    assert samples["b"].values == (5.0, 7.0)


def test_compare_speedup_signs(tmp_path: Path) -> None:
    """A faster candidate gets a negative delta and a speedup over 1, a slower one the opposite."""
    other, base = _write_pair(
        tmp_path,
        other=[_row("bench_x", 80.0), _row("bench_y", 75.0)],
        base=[_row("bench_x", 100.0), _row("bench_y", 50.0)],
    )

    console = Console(width=200)
    code = compare([base, other], console=console)
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
    """`compare` warns about benchmarks missing from one file and leaves them out of the table."""
    other, base = _write_pair(
        tmp_path,
        other=[_row("bench_x", 80.0)],
        base=[_row("bench_x", 100.0), _row("bench_only_in_base", 1.0)],
    )

    console = Console(width=200)
    code = compare([base, other], console=console)
    assert code == 0
    err = capsys.readouterr().err
    assert "bench_only_in_base" in err
    out = console.export_text()
    assert "bench_x" in out
    assert "bench_only_in_base" not in out


def test_compare_empty_overlap(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """`compare` exits with code 1 when the files share no benchmarks."""
    other, base = _write_pair(tmp_path, other=[_row("b", 1.0)], base=[_row("a", 1.0)])
    code = compare([base, other], console=Console())
    assert code == 1
    assert "no overlapping benchmarks" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("pattern", "literal", "shown"),
    [("alpha|gamma", False, ["alpha", "gamma"]), ("beta[n=1]", True, ["beta[n=1]"])],
)
def test_compare_pattern_is_a_regex_unless_literal(
    tmp_path: Path, pattern: str, literal: bool, shown: list[str]
) -> None:
    """`pattern` filters benchmark names as a regex, or as plain text with `literal=True`."""
    names = ["alpha", "beta[n=1]", "gamma"]
    other, base = _write_pair(
        tmp_path, other=[_row(n, 5.0) for n in names], base=[_row(n, 10.0) for n in names]
    )
    console = Console(width=200)
    assert compare([base, other], pattern=pattern, literal=literal, console=console) == 0
    out = console.export_text()
    assert [n for n in names if n in out] == shown


@pytest.mark.parametrize(
    ("n_files", "kwargs", "match"),
    [
        (1, {}, "at least two"),
        (2, {"metric": "bogus"}, "unknown metric"),
        (2, {"metric": "counters."}, "unknown metric"),
        (2, {"pattern": "foo("}, "invalid benchmark filter pattern"),
    ],
)
def test_compare_rejects_bad_arguments(
    tmp_path: Path, n_files: int, kwargs: dict, match: str
) -> None:
    """`compare` rejects too few files, unknown metrics and keys, and bad patterns."""
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


@pytest.mark.parametrize("metric", ["real_time", "memory.peak_bytes"])
def test_compare_values_carry_their_spread(tmp_path: Path, metric: str) -> None:
    """Each value shows its repetitions' spread relative to their mean, whatever the metric's
    unit; one past 25% is flagged as too noisy to trust, and a single repetition has none."""
    mib = 1 << 20
    other, base = _write_pair(
        tmp_path,
        other=[_mem_row("b", v, peak=v * mib, allocs=1) for v in (2, 4, 6)]
        + [_mem_row("once", 1, peak=mib, allocs=1)],
        base=[_mem_row("b", v, peak=v * mib, allocs=1) for v in (4, 5, 6)]
        + [_mem_row("once", 1, peak=mib, allocs=1)],
    )
    console = Console(width=200)
    assert compare([base, other], metric=metric, console=console) == 0
    rows = {
        cells[0]: cells
        for line in console.export_text().splitlines()
        if (cells := [c.strip() for c in line.split("│")])[0] in ("b", "once")
    }
    assert rows["b"][1].endswith(" ±20%")  # stdev 1 of mean 5
    assert rows["b"][2].endswith(" ±50% (!)")  # stdev 2 of mean 4
    assert "±" not in rows["once"][1]


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
        # Every row carries the result format it was written in.
        ("a.jsonl", _ROW_LINE + '\n{"name": "x"}\n', r"a.jsonl:2: .*\(no schema\)"),
        (
            "a.jsonl",
            _ROW_LINE + '\n{"name": "x", "schema": "v2"}\n',
            r"\(schema 'v2'\); this mew reads schema 'v1'",
        ),
        (
            "a.jsonl",
            _ROW_LINE + '\n{"schema": "v1"}\n',
            "a.jsonl:2: expected a mew result row",
        ),
        # Cut off, as by a full disk or an interrupted copy.
        # A fixed gzip timestamp keeps the test ID stable.
        ("a.jsonl.gz", gzip.compress(_ROW_LINE.encode(), mtime=0)[:-8], "cannot read result file"),
    ],
)
def test_load_rejects_unreadable_files(
    tmp_path: Path, filename: str, content: str | bytes | None, match: str
) -> None:
    """`load_column` rejects missing, unsupported, malformed and truncated result files."""
    # A one-line error naming the file (and line), which the CLI prints.
    p = tmp_path / filename
    if isinstance(content, bytes):
        p.write_bytes(content)
    elif content is not None:
        p.write_text(content)
    with pytest.raises(FileNotFoundError if content is None else ValueError, match=match):
        load_column(p, "real_time")


@pytest.mark.parametrize("suffix", [".json", ".jsonl", ".jsonl.gz"])
def test_compare_reads_every_result_format(tmp_path: Path, suffix: str) -> None:
    """`compare` reads `.json`, `.jsonl` and `.jsonl.gz` result files."""
    other, base = _write_pair(
        tmp_path, other=[_row("bench_x", 50.0)], base=[_row("bench_x", 100.0)], suffix=suffix
    )
    console = Console(width=200)
    assert compare([base, other], console=console) == 0
    out = console.export_text()
    assert "-50.00%" in out and "×2.000" in out


def test_same_second_runs_stay_distinct_and_order_by_run_id(tmp_path: Path) -> None:
    """Two runs in the same second stay separate runs, ordered by their time-ordered id."""
    p = tmp_path / "agg.json"
    when = "2026-06-12T10:00:00"
    _write_json(
        p,
        [
            _row("b", 10.0, date=when, run_id="0197-aaaa", run_name="before"),
            _row("b", 20.0, date=when, run_id="0197-bbbb", run_name="after"),
        ],
    )
    assert [s.id for s in run_summaries(p)] == ["0197-bbbb", "0197-aaaa"]


def test_a_file_pools_every_run(tmp_path: Path, capsys) -> None:
    """A file is one sample: the runs appended to it pool as repetitions, e.g. the rounds of
    an interleaved A/B loop. Provenance comes from the newest run, with the count of runs.
    Runs from one machine, interpreter and commit pool quietly."""
    p = tmp_path / "r.jsonl"
    same = {"cpu_model": "M2", "python": {"version": "3.13"}, "vcs": {"commit": "abc"}}
    _write_jsonl(
        p,
        [
            _row("b", 10.0, date="2026-01-01T00:00:00", run_id="aaaa", custom=same),
            _row("b", 20.0, date="2026-02-01T00:00:00", run_id="bbbb", custom=same),
        ],
    )
    pooled, ctx, _ = load_column(p, "real_time")
    assert pooled["b"].values == (10.0, 20.0)
    assert (ctx["run"]["id"], ctx["runs"]) == ("bbbb", 2)
    assert capsys.readouterr().err == ""
    base = tmp_path / "base.json"
    _write_json(base, [_row("b", 15.0)])
    console = Console(width=200)
    compare([base, p], console=console)
    assert "2 runs, newest 2026-02-01" in console.export_text()


@pytest.mark.parametrize(
    ("other", "field"),
    [
        ({"cpu_model": "M4"}, "cpu_model"),
        ({"python": {"version": "3.15"}}, "python"),
        ({"vcs": {"commit": "def"}}, "commit"),
    ],
)
def test_pooled_runs_warn_when_they_differ(tmp_path: Path, capsys, other, field) -> None:
    """Pooling runs from another machine, interpreter or commit warns: their repetitions mix
    environments, as an archive appended to over months would."""
    p = tmp_path / "r.jsonl"
    base = {"cpu_model": "M2", "python": {"version": "3.13"}, "vcs": {"commit": "abc"}}
    _write_jsonl(
        p,
        [
            _row("b", 10.0, date="2026-01-01T00:00:00", run_id="aaaa", custom=base),
            _row("b", 20.0, date="2026-02-01T00:00:00", run_id="bbbb", custom={**base, **other}),
        ],
    )
    load_column(p, "real_time")
    assert f"r.jsonl pools 2 runs that differ in {field}" in capsys.readouterr().err


def test_load_of_an_empty_file_errors_naming_it(tmp_path: Path) -> None:
    """An empty archive, as an interrupted first run leaves, raises a `ValueError` that names
    the file rather than failing on an empty sequence."""
    p = tmp_path / "r.jsonl"
    p.write_text("")
    with pytest.raises(ValueError, match=f"^{re.escape(str(p))}: no measurements in file"):
        load_column(p, "real_time")


def test_compare_prints_context_and_warns_on_skew(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`compare` labels columns with differing context keys, and warns on machine skew, including
    a `host` that a suite records."""
    other, base = _write_pair(
        tmp_path,
        other=[_row("bench_x", 80.0)],
        base=[_row("bench_x", 100.0)],
        other_context={
            "context": {
                "host": "h2",
                "cpu_model": "Apple M2",
                "num_cpus": 4,
                "engine": "duckdb 1.5.3",
            },
        },
        base_context={
            "context": {
                "host": "h1",
                "cpu_model": "Apple M1",
                "num_cpus": 8,
                "engine": "ducky 0.1",
            },
        },
    )
    console = Console(width=200)
    assert compare([base, other], console=console) == 0
    out = console.export_text()
    assert "host=h1" in out
    assert "host=h2" in out
    # Differing custom keys annotate the column labels.
    assert "engine=ducky 0.1" in out
    assert "engine=duckdb 1.5.3" in out
    err = capsys.readouterr().err
    assert "differ in host (base: h1, other: h2)" in err
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
        other_context={"context": {**machine, "python": {**python, "version": "3.15.0"}}},
        base_context={"context": {**machine, "python": {**python, "version": "3.14.3"}}},
    )
    console = Console(width=200)
    assert compare([base, other], console=console) == 0
    out = console.export_text()
    assert "python.version=3.14.3" in out
    assert "python.version=3.15.0" in out
    # What both files share goes unsaid, and isn't warned on.
    assert "cpu_model" not in out
    assert "differ in" not in capsys.readouterr().err


def test_compare_memory_metric(tmp_path: Path) -> None:
    """Memory metrics show byte-formatted values under a `ratio` column instead of `speedup`."""
    other, base = _write_pair(
        tmp_path,
        other=[_mem_row("bench_x", 1.0, peak=1 << 21, allocs=50)],
        base=[_mem_row("bench_x", 1.0, peak=1 << 20, allocs=100)],
    )

    console = Console(width=200)
    code = compare([base, other], metric="memory.peak_bytes", console=console)
    assert code == 0
    out = console.export_text()
    assert "1.0 MB" in out  # byte-formatted baseline
    assert "+100.00%" in out
    # Memory isn't a speedup: the ratio column is neutrally labelled.
    assert "ratio" in out
    assert "speedup" not in out

    console = Console(width=200)
    code = compare([base, other], metric="memory.allocations_per_iteration", console=console)
    assert code == 0
    assert "-50.00%" in console.export_text()


@pytest.mark.parametrize(
    ("counter", "rules", "base", "other", "code"),
    [
        # More is better, e.g. a GPU transfer rate: falling is the regression.
        ("work", [CounterRule("w*", True)], 200, 100, 3),
        ("work", [CounterRule("w*", True)], 100, 200, 0),
        # Less is better, e.g. TLB misses: rising is the regression.
        ("work", [CounterRule("w*", False)], 100, 200, 3),
        ("work", [CounterRule("w*", False)], 200, 100, 0),
        # Throughputs are higher-is-better without a rule, and a rule overrides that.
        ("items_per_second", [], 200, 100, 3),
        ("items_per_second", [CounterRule("items_per_second", False)], 200, 100, 0),
    ],
)
def test_compare_counter_gates_in_its_configured_direction(
    tmp_path: Path, counter: str, rules: list[CounterRule], base: float, other: float, code: int
) -> None:
    """`counters.<name>` gates in the direction `CounterConfig` gives it."""
    other_path, base_path = _write_pair(
        tmp_path, other=[counter_row("b", counter, other)], base=[counter_row("b", counter, base)]
    )
    result = compare(
        [base_path, other_path],
        metric=f"counters.{counter}",
        regressions=RegressionConfig(5),
        counters=CounterConfig(rules=tuple(rules)),
        console=Console(),
    )
    assert result == code


def test_compare_refuses_to_gate_on_counters_without_a_direction(tmp_path: Path) -> None:
    """Gating on an undeclared counter fails instead of passing unchecked."""
    other, base = _write_pair(
        tmp_path, other=[counter_row("b", "work", 0)], base=[counter_row("b", "work", 1)]
    )
    with pytest.raises(ValueError, match="no direction is declared"):
        compare([base, other], metric="counters.work", regressions=RegressionConfig(5))


def test_compare_counter_without_a_direction_shows_a_plain_ratio(tmp_path: Path) -> None:
    """Ungated, an undeclared counter compares as `other / base`, its delta neither good nor bad."""
    other, base = _write_pair(
        tmp_path, other=[counter_row("b", "work", 200)], base=[counter_row("b", "work", 100)]
    )
    console = Console(width=200)
    assert compare([base, other], metric="counters.work", console=console) == 0
    out = console.export_text()
    assert "+100.00%" in out
    assert "×2.000" in out
    assert "ratio" in out
    assert "speedup" not in out


def test_compare_counter_formats_like_the_console(tmp_path: Path) -> None:
    """Counter values show with the console's prefixes and rate suffixes."""
    rate = {"rate": True, "one_k": 1024}
    other, base = _write_pair(
        tmp_path,
        other=[counter_row("b", "work", v * 2**30, format=rate) for v in (2, 3, 4)],
        base=[counter_row("b", "work", v * 2**30, format=rate) for v in (1, 1, 1)],
    )
    console = Console(width=200)
    assert compare([base, other], metric="counters.work", console=console) == 0
    out = console.export_text()
    assert "1Gi/s" in out  # baseline value
    assert "3Gi/s" in out  # contender median
    assert "+200.00%" in out


def test_compare_missing_counter_hints_set_counter(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A counter no benchmark set exits 1 and names the counter."""
    other, base = _write_pair(
        tmp_path, other=[counter_row("b", "work", 1.0)], base=[counter_row("b", "work", 1.0)]
    )
    assert compare([base, other], metric="counters.missing", console=Console()) == 1
    err = capsys.readouterr().err
    assert "'missing'" in err
    assert "set_counter" in err


def test_compare_memory_metric_without_data_hints_profile_flag(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A memory metric on results without memory data exits 1 and points to
    `--memory-profiler memray`."""
    other, base = _write_pair(tmp_path, other=[_row("bench_x", 1.0)], base=[_row("bench_x", 1.0)])
    code = compare([base, other], metric="memory.peak_bytes", console=Console())
    assert code == 1
    assert "--memory-profiler memray" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("other", "base", "markers"),
    [
        # Baseline reps scatter wildly (CV >> 25%): stdev([40,100,160])/mean = 60/100.
        ((99.0, 100.0, 101.0), (40.0, 100.0, 160.0), ["±60% (!)"]),
        # Both sides steady.
        ((49.0, 50.0, 51.0), (99.0, 100.0, 101.0), []),
    ],
)
def test_compare_marks_only_high_cv_rows(
    tmp_path: Path,
    other: tuple[float, ...],
    base: tuple[float, ...],
    markers: list[str],
) -> None:
    """Only values whose repetitions vary by more than 25% of the mean get a `±N% (!)` marker."""
    other_path, base_path = _write_pair(
        tmp_path, other=[_row("b", v) for v in other], base=[_row("b", v) for v in base]
    )
    console = Console(width=200)
    assert compare([base_path, other_path], console=console) == 0
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
    """Only a statistically significant delta gets the `(signif.)` marker."""
    other_path, base_path = _write_pair(
        tmp_path, other=[_row("b", v) for v in other], base=[_row("b", v) for v in base]
    )
    console = Console(width=200)
    assert compare([base_path, other_path], console=console) == 0
    assert ("(signif.)" in console.export_text()) is significant


def test_fmt_delta_colors_by_improvement_direction() -> None:
    """`_fmt_delta` colors a delta by improvement direction and shows an infinite one as `+∞%`."""
    assert _fmt_delta(0.2) == ("+20.00%", "red")  # slower time: bad
    assert _fmt_delta(-0.2) == ("-20.00%", "green")
    # Where up is better, as for a throughput counter, a rise is an improvement.
    assert _fmt_delta(0.2, higher_is_better=True)[1] == "green"
    assert _fmt_delta(-0.2, higher_is_better=True)[1] == "red"
    assert _fmt_delta(0.0) == ("+0.00%", "")
    assert _fmt_delta(0.0, higher_is_better=True) == ("+0.00%", "")
    # A counter without a direction: neither good nor bad.
    assert _fmt_delta(0.2, higher_is_better=None) == ("+20.00%", "")
    # A zero baseline against a nonzero contender must not read as +0.00%.
    assert _fmt_delta(math.inf) == ("+∞%", "red")


def test_read_results_returns_rows_as_stored(tmp_path: Path) -> None:
    """The raw view: nothing filtered, rows self-describing."""
    p = tmp_path / "r.json"
    _write_json(
        p,
        [
            _row("b", 10.0, run_id="s1", custom={"engine": "x"}),
            _row("b", 0.0, skipped=True, run_id="s1"),
        ],
    )
    rows = read_results(p)
    assert len(rows) == 2, "skipped rows are not filtered here"
    assert rows[0]["run"]["id"] == "s1"
    assert rows[0]["context"]["engine"] == "x"


@pytest.mark.parametrize(("gate", "code"), [(None, 0), (RegressionConfig(5), 3)])
def test_rows_are_the_benchmarks_candidate_and_baseline_share(
    tmp_path: Path, gate: RegressionConfig | None, code: int
) -> None:
    """Gated or not, one row set: a benchmark the history lacks still shows, so it
    can't hide a regression, and one the candidate lacks has nothing to compare."""
    head, history, baseline = [tmp_path / f"{name}.json" for name in ("head", "history", "base")]
    _write_json(head, [_row("slow", 200), _row("ok", 100)])
    _write_json(history, [_row("ok", 100), _row("gone", 100)])
    _write_json(baseline, [_row("slow", 100), _row("ok", 100), _row("gone", 100)])
    console = Console(width=200)
    files = [baseline, head, history]  # the baseline first; the second file is gated
    assert compare(files, regressions=gate, console=console) == code
    lines = console.export_text().splitlines()
    assert [line.split("│")[0].strip() for line in lines if "│" in line][1:] == ["ok", "slow"]


@pytest.mark.parametrize(
    ("base_value", "other_value", "higher_is_better", "delta", "ratio"),
    [
        (100, 200, True, 1, 2),  # e.g. items_per_second
        (100, 200, False, 1, 0.5),  # e.g. memory.peak_bytes
        (0, 1, False, float("inf"), 0),
        (1, 0, False, -1, float("inf")),
        # Both zero (e.g. allocation-free bodies) is a tie, not ×inf.
        (0, 0, False, 0, 1),
    ],
)
def test_comparison_math_without_rendering(base_value, other_value, higher_is_better, delta, ratio):
    """`_compare_samples` inverts the ratio by direction and handles zeros."""
    result = _compare_samples(Sample(base_value), Sample(other_value), higher_is_better)
    assert result.delta == delta
    assert result.speedup == ratio
    assert result.p_value is None


@pytest.mark.parametrize(
    ("base", "other", "delta", "text", "style"),
    [
        (-10.0, -5.0, 0.5, "+50.00%", "green"),  # a rise, so better
        (-10.0, -20.0, -1.0, "-100.00%", "red"),
        (0.0, -5.0, -math.inf, "-∞%", "red"),
        (0.0, 5.0, math.inf, "+∞%", "green"),
    ],
)
def test_counter_delta_keeps_its_sign_at_or_below_zero(base, other, delta, text, style) -> None:
    """A delta's sign follows the change, not the baseline's sign, so the gate sees a fall."""
    r = _compare_samples(Sample(base), Sample(other), True)
    assert r.delta == delta
    assert _fmt_delta(r.delta, higher_is_better=True) == (text, style)
    regressed = RegressionConfig().evaluate("x", r.delta * 100, higher_is_better=True).verdict
    assert (regressed.name == "REGRESSED") == (style == "red")
    # A ratio of a negative counter means nothing; zero alone keeps one.
    assert (r.speedup is None) == (base < 0 or other < 0)


def test_compare_shows_no_ratio_for_a_negative_counter(tmp_path: Path) -> None:
    """The ratio cell of a counter that is negative on either side is a dash."""
    other, base = _write_pair(
        tmp_path,
        other=[counter_row("b", "balance", -5.0)],
        base=[counter_row("b", "balance", -10.0)],
    )
    console = Console(width=120)
    rule = CounterRule(pattern="balance", higher_is_better=True)
    counters = CounterConfig(rules=(rule,))
    compare([base, other], metric="counters.balance", counters=counters, console=console)
    row = next(line for line in console.export_text().splitlines() if line.startswith("b "))
    assert "+50.00%" in row
    assert row.rstrip().endswith("—")


def test_compare_filter_matching_nothing_says_so(tmp_path: Path, capsys) -> None:
    """A `-k` pattern that matches nothing says that, not that the files don't overlap."""
    other, base = _write_pair(tmp_path, other=[_row("b", 1.0)], base=[_row("b", 1.0)])
    assert compare([base, other], pattern="nomatch", console=Console()) == 1
    assert capsys.readouterr().err.strip() == "no benchmarks match 'nomatch'"


def test_a_file_of_only_skipped_rows_has_no_measurements(tmp_path: Path) -> None:
    """A file whose runs skipped everything is an error naming the file, not an empty column."""
    p = tmp_path / "r.jsonl"
    _write_jsonl(
        p,
        [
            _row("b", 0.0, date="2026-01-01T00:00:00", run_id="old", skipped=True),
            _row("b", 0.0, date="2026-02-01T00:00:00", run_id="new", skipped=True),
        ],
    )
    with pytest.raises(ValueError, match=re.escape("r.jsonl: no measurements, only skipped rows")):
        load_column(p, "real_time")


def test_unreadable_values_name_the_file(tmp_path: Path) -> None:
    """A file that isn't UTF-8, or a value that isn't a number, gives an error naming the file."""
    binary = tmp_path / "bin.json"
    binary.write_bytes(b"\xff\xfe{}")
    with pytest.raises(ValueError, match=f"cannot read result file {re.escape(str(binary))}"):
        load_column(binary, "real_time")
    bad = tmp_path / "bad.json"
    _write_json(bad, [_row("b", 1.0) | {"real_time": "fast"}])
    with pytest.raises(ValueError, match=r"bad\.json: 'b': real_time is 'fast', not a number"):
        load_column(bad, "real_time")


def test_compare_tells_skipped_benchmarks_from_missing_ones(tmp_path: Path, capsys) -> None:
    """A benchmark the run skipped is reported with its reason; one not in the file at all is
    missing."""
    base, head = tmp_path / "base.json", tmp_path / "head.json"
    _write_json(base, [_row("a", 1.0), _row("gpu", 1.0), _row("new", 0.0, skipped=True)])
    reason = {"reason": "skipped", "message": "no GPU"}
    _write_json(head, [_row("a", 1.0), _row("gpu", 0.0, skip=reason), _row("new", 1.0)])
    other = tmp_path / "other.json"
    _write_json(other, [_row("a", 1.0)])
    assert compare([base, head, other], console=Console(width=200)) == 0
    err = capsys.readouterr().err
    assert f"skipped in {head}: gpu (no GPU)" in err
    assert f"skipped in {base}: new (skipped)" in err
    assert f"{other} missing 2 benchmark(s): gpu, new" in err


@pytest.mark.parametrize(
    ("names", "title", "rows"),
    [
        (["b.py::x", "b.py::y"], "Comparison of b.py (real_time", ["x", "y"]),
        (["a.py::x", "b.py::y"], "Comparison (real_time", ["a.py::x", "b.py::y"]),
    ],
)
def test_compare_names_a_shared_file_once_in_the_title(tmp_path: Path, names, title, rows) -> None:
    """When every row comes from one file, the file goes in the title and the rows drop it."""
    base, other = tmp_path / "base.json", tmp_path / "other.json"
    for p in (base, other):
        _write_json(p, [_row(n, 1.0) for n in names])
    console = Console(width=200)
    assert compare([base, other], console=console) == 0
    out = console.export_text()
    assert title in out
    lines = [line for line in out.splitlines() if "│" in line][1:]
    assert [line.split("│")[0].strip() for line in lines] == rows


@pytest.mark.parametrize(
    ("other", "metric", "summary"),
    [
        # 200 -> 400 for `slow`, unchanged `ok`, 5 repetitions a side: one significant change.
        (
            {"slow": 400.0, "ok": 100.0},
            "real_time",
            "1 slower, 0 faster, 1 not significantly different",
        ),
        (
            {"slow": 100.0, "ok": 100.0},
            "real_time",
            "0 slower, 1 faster, 1 not significantly different",
        ),
    ],
)
def test_compare_sums_up_the_second_file_against_the_baseline(
    tmp_path: Path, other, metric, summary
) -> None:
    """A line under the table counts the benchmarks that changed significantly, by direction."""
    base_path, other_path = tmp_path / "base.json", tmp_path / "head.json"
    jitter = (0.99, 1.0, 1.01, 0.995, 1.005)
    _write_json(
        base_path, [_row(n, v * j) for n, v in {"slow": 200.0, "ok": 100.0}.items() for j in jitter]
    )
    _write_json(other_path, [_row(n, v * j) for n, v in other.items() for j in jitter])
    console = Console(width=200)
    compare([base_path, other_path], metric=metric, console=console)
    assert console.export_text().splitlines()[-1] == f"head against base: {summary}"


def test_compare_summary_without_repetitions_says_there_is_no_test(tmp_path: Path) -> None:
    """With one repetition a side there is no significance test, and the summary says so."""
    other, base = _write_pair(tmp_path, other=[_row("b", 2.0)], base=[_row("b", 1.0)])
    console = Console(width=200)
    compare([base, other], console=console)
    last = console.export_text().splitlines()[-1]
    assert (
        last
        == "other against base: 1 compared; no significance test, which needs 2+ repetitions a side"
    )

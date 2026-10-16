"""Reporters: JSON/JSONL row shape and streaming, and the RichReporter table."""

from __future__ import annotations

import io
import json
from typing import cast

import pytest
from _helpers import Capture

import mew
from mew import BenchmarkResult
from mew.compare import read_results
from mew.reporter import JSONLReporter, JSONReporter, RichReporter


def _run_one(reporter):
    @mew.benchmark
    def bench_x(state):
        for _ in state:
            pass

    mew.run(min_time="1x", reporter=reporter)


def test_json_reporter_writes_to_file(tmp_path):
    out = tmp_path / "results.json"
    rep = JSONReporter(output=out)
    _run_one(rep)

    doc = json.loads(out.read_text())
    assert "context" in doc and "benchmarks" in doc
    assert len(doc["benchmarks"]) == 1
    bench = doc["benchmarks"][0]
    for key in (
        "name",
        "iterations",
        "real_time",
        "cpu_time",
        "time_unit",
        "threads",
        "run_type",
    ):
        assert key in bench, key


def test_json_reporter_writes_to_stream():
    buf = io.StringIO()
    rep = JSONReporter(output=buf)
    _run_one(rep)
    doc = json.loads(buf.getvalue())
    assert doc["benchmarks"][0]["iterations"] >= 1


@pytest.mark.parametrize(
    ("reporter", "suffix"), [(JSONReporter, ".json"), (JSONLReporter, ".jsonl")]
)
def test_file_reporter_can_be_reused(tmp_path, reporter, suffix):
    # A second run reopens the sink finalize() closed, and starts it over.
    out = tmp_path / f"results{suffix}"
    rep = reporter(output=out)

    @mew.benchmark
    def bench_reuse(state):
        for _ in state:
            pass

    for _ in range(2):
        mew.run(min_time="1x", reporter=rep)
        assert len(read_results(out)) == 1


def _fake_row(name: str, label: str = "", benchmark: str | None = None) -> BenchmarkResult:
    """A minimal BenchmarkResult row for driving reporters without a run."""
    return {
        "name": name,
        "run_name": name,
        "benchmark": benchmark or name,
        "family_index": 0,
        "per_family_instance_index": 0,
        "run_type": "iteration",
        "aggregate_name": "",
        "repetitions": 1,
        "repetition_index": 0,
        "threads": 1,
        "iterations": 1,
        "real_time": 1.0,
        "cpu_time": 1.0,
        "real_accumulated_time": 0.0,
        "cpu_accumulated_time": 0.0,
        "time_unit": "ns",
        "label": label,
        "skipped": False,
        "skip_message": "",
        "counters": [],
    }


def test_json_reporter_streams_forward_only(tmp_path):
    """Rows land on disk as they arrive, like GB; the document only parses after
    finalize (JSONL is the interruption-safe format)."""
    out = tmp_path / "results.json"
    rep = JSONReporter(output=out)
    rep.report_context({"session": {"host": "h"}, "context": {"num_cpus": 4}})

    rep.report_runs([_fake_row("a::one"), _fake_row("a::two")])
    partial = out.read_text()
    assert "a::one" in partial and "a::two" in partial  # streamed, not buffered
    with pytest.raises(json.JSONDecodeError):
        json.loads(partial)  # open-ended until finalize, like GB itself

    rep.report_runs([_fake_row("a::three")])
    rep.finalize()
    doc = json.loads(out.read_text())
    assert doc["session"]["host"] == "h"
    assert [b["name"] for b in doc["benchmarks"]] == ["a::one", "a::two", "a::three"]


def test_jsonl_reporter_duckdb_query_round_trip(tmp_path):
    # The archive contract: self-contained NDJSON rows are directly queryable
    # by DuckDB, with nested blocks (`custom`) arriving as structs.
    duckdb = pytest.importorskip("duckdb")

    import mew

    mew.set_context("dataset.size", 1024)

    out = tmp_path / "results.jsonl"
    _run_one(JSONLReporter(output=out))

    con = duckdb.connect()
    rows = con.execute(
        f"SELECT name, real_time, context.dataset.size, session.id FROM '{out}'"
    ).fetchall()
    assert len(rows) == 1
    name, real_time, sz, session_id = rows[0]
    assert "bench_x" in name
    # One empty iteration can be shorter than a clock tick, so 0 is possible.
    assert real_time == json.loads(out.read_text())["real_time"]
    assert sz == 1024
    assert session_id  # identity on the row itself, no header join


def test_jsonl_gz_reporter_duckdb_query_round_trip(tmp_path):
    duckdb = pytest.importorskip("duckdb")

    out = tmp_path / "results.jsonl.gz"
    _run_one(JSONLReporter(output=out))

    (row,) = duckdb.connect().execute(f"SELECT name FROM read_json_auto('{out}')").fetchall()
    assert "bench_x" in row[0]


def test_jsonl_reporter_gzips_an_uppercase_gz_suffix(tmp_path):
    # The CLI and the reader match `.jsonl.gz` in any case, so the writer must too.
    out = tmp_path / "results.JSONL.GZ"
    _run_one(JSONLReporter(output=out))

    assert out.read_bytes()[:2] == b"\x1f\x8b"  # the gzip magic number
    (row,) = read_results(out)
    assert "bench_x" in row["name"]


def test_rich_reporter_streams_header_before_first_run():
    from mew._console import Terminal

    buf = io.StringIO()
    rep = RichReporter(terminal=Terminal(file=buf, width=120, color=False))
    rep.report_context(
        {"session": {"host": "h"}, "context": {"num_cpus": 4, "cpu_scaling_enabled": False}}
    )
    out = buf.getvalue()
    assert "host=h cpus=4 scaling=disabled" in out
    assert "Benchmark" in out
    assert "Iters" in out


def _rich(width: int = 200, **flags) -> tuple[io.StringIO, RichReporter]:
    from mew._console import Terminal

    buf = io.StringIO()
    rep = RichReporter(terminal=Terminal(file=buf, width=width, color=False), **flags)
    rep.report_context(
        {"session": {"host": "h"}, "context": {"num_cpus": 1, "cpu_scaling_enabled": False}}
    )
    return buf, rep


@pytest.mark.parametrize(
    ("name_width", "column"),
    [
        (25, 25),  # the longest name to come
        (None, 159),  # unknown: all the width the numbers leave
        (500, 159),  # never past the terminal
        (5, 20),  # never below the floor
    ],
)
def test_rich_reporter_sizes_the_name_column(name_width, column):
    buf, _ = _rich(width=200, name_width=name_width)
    header = buf.getvalue().splitlines()[1]
    assert len(header.split(" │ ")[0]) == column


def test_rich_reporter_scales_times_to_a_readable_unit():
    buf, rep = _rich()
    row = _fake_row("bench.py::bench_x")
    row["real_time"], row["cpu_time"] = 305_028_333.33, 24_763.67
    rep.report_runs([row])
    cells = [c.strip() for c in buf.getvalue().splitlines()[-1].split(" │ ")]
    assert cells[2:] == ["305.03 ms", "24.76 us"]


def test_rich_reporter_memory_column_and_profile_line():
    buf, rep = _rich(show_memory=True, show_cpu=True)
    header = buf.getvalue().splitlines()[1]
    assert header.split(" │ ")[-1].strip() == "Peak Mem"

    row = _fake_row("bench.py::bench_x")
    row["memory"] = {"peak_bytes": 2 * (1 << 20)}  # 2.0 MB
    row["cpu_profile"] = {
        "sample_count": 4200,
        "top_function": {"function": "hot_fn", "file": "mod.py", "line": 10, "self_time": 0.1},
    }
    rep.report_runs([row, _fake_row("bench.py::bench_y")])
    lines = buf.getvalue().splitlines()[-4:]
    assert lines[0].split(" │ ")[-1].strip() == "2.0 MB"
    assert lines[1] == "  4,200 samples · hottest hot_fn (mod.py:10)"
    # A row whose pass sampled nothing says so rather than leaving a gap.
    assert lines[2].split(" │ ")[-1].strip() == "-"
    assert lines[3] == "  no samples"


def test_rich_reporter_lists_what_has_no_column_below_the_row():
    """A label the body set, the counters and the profile go below the row,
    filled to the terminal width without splitting a part."""
    buf, rep = _rich(width=64, show_cpu=True)
    row = _fake_row("bench.py::bench_x", label="warm-cache")
    row["counters"] = [{"name": "items_per_second", "value": 2.5e8, "format": {"rate": True}}]
    row["cpu_profile"] = {
        "sample_count": 1,
        "top_function": {
            "function": "a_very_long_function_name_that_exceeds_thirty_characters",
            "file": "mod.py",
            "line": 1,
            "self_time": 0.1,
        },
    }
    # A family case's own label is already in its name, so it isn't repeated.
    case = _fake_row("bench.py::bench_x[n=1]", label="n=1")
    rep.report_runs([row, case])
    lines = buf.getvalue().splitlines()[3:]
    assert lines[1:3] == [
        "  warm-cache · items_per_second=250M/s · 1 sample",
        # A part too long for a line of its own is cut, not split.
        "    hottest a_very_long_function_name_that_exceeds_thirty_chara…",
    ]
    assert all(len(line) <= 64 for line in lines)
    assert lines[-1] == "  no samples"  # the case row: no label repeated


def test_rich_reporter_wraps_a_skip_message_without_losing_any_of_it():
    buf, rep = _rich(width=60)
    path = "/a/very/long/path/" + "x" * 50 + "/bench.py"
    row = _fake_row("bench.py::bench_x")
    row["skipped"] = True
    row["skip_message"] = (
        f'Traceback (most recent call last):\n  File "{path}", line 6\nValueError: boom'
    )
    rep.report_runs([row])
    lines = buf.getvalue().splitlines()[3:]
    assert all(len(line) <= 60 for line in lines)
    # The reason fills the timing columns: past the 20-column name and the separator.
    body = [line[23:] for line in lines]
    assert body[0] == "Traceback (most recent call last):"
    assert body[1].startswith('  File "/a/very')  # the traceback's own indent stays
    assert "".join(body[1:-1]).replace(" ", "") == f'File"{path}",line6'
    assert body[-1] == "ValueError: boom"


@pytest.mark.parametrize(
    ("top", "shown"),
    [
        ({"function": "f", "file": "b.py", "line": 6, "self_time": 0.1}, "f (b.py:6)"),
        # pyinstrument knows no hottest line, a synthetic frame no file.
        ({"function": "f", "file": "b.py", "line": None, "self_time": 0.1}, "f (b.py)"),
        ({"function": "<GC>", "file": None, "line": None, "self_time": 0.1}, "<GC>"),
        # A custom profiler's free-form summary.
        ("custom (x.py:2)", "custom (x.py:2)"),
    ],
)
def test_rich_reporter_renders_the_hottest_frame(top, shown):
    buf, rep = _rich(show_cpu=True)
    row = _fake_row("bench.py::bench_x")
    row["cpu_profile"] = {"sample_count": 1, "top_function": top}
    rep.report_runs([row])
    assert buf.getvalue().splitlines()[-1] == f"  1 sample · hottest {shown}"


def test_rich_reporter_lists_the_hottest_instruction_with_its_shares():
    buf, rep = _rich(show_cpu=True)
    profiles = [
        ("BINARY_OP", 0.62, 0.62),
        ("LOAD_ATTR_INSTANCE_VALUE", 0.32, 0.0),
        ("CALL_METHOD_DESCRIPTOR_FAST_WITH_KEYWORDS", 0.4, 0.1),
    ]
    for name, share, generic in profiles:
        row = _fake_row("bench.py::bench_x")
        row["cpu_profile"] = {
            "sample_count": 1,
            "top_function": {"function": "f", "file": "b.py", "line": 1, "self_time": 0.1},
            # Only the first, hottest entry reaches the console.
            "top_instructions": [
                {"instruction": name, "share": share},
                {"instruction": "STORE_FAST", "share": 0.01},
            ],
            "unspecialized_share": generic,
        }
        rep.report_runs([row])

    lines = buf.getvalue().splitlines()[4::2]
    assert [line.split(" · ")[-1] for line in lines] == [
        "BINARY_OP 62%, 62% generic",
        "LOAD_ATTR_INSTANCE_VALUE 32%",
        "CALL_METHOD_DESCRIPTOR_FAST_WITH_KEYWORDS 40%, 10% generic",
    ]


def test_rich_reporter_renders_the_benchmark_field():
    """The live table shows the row's `benchmark` (the `name[label]` form), not
    GB's raw `/case:N/min_time:…` name, so it reads the same as `mew compare`."""
    from mew._console import Terminal

    buf = io.StringIO()
    rep = RichReporter(terminal=Terminal(file=buf, width=120, color=False))
    rep.report_context(
        {"session": {"host": "h"}, "context": {"num_cpus": 4, "cpu_scaling_enabled": False}}
    )
    rep.report_runs(
        [
            _fake_row(
                "bench.py::bench_x/case:0/min_time:0.200",
                label="small",
                benchmark="bench.py::bench_x[small]",
            )
        ]
    )
    out = buf.getvalue()
    assert "bench.py::bench_x[small]" in out
    assert "case:0" not in out
    assert "min_time" not in out


def test_jsonl_reporter_flushes_incrementally(tmp_path):
    out = tmp_path / "partial.jsonl"
    rep = JSONLReporter(output=out)
    rep.report_context({})

    rep.report_runs([_fake_row("f::bench")])
    lines = [ln for ln in out.read_text().splitlines() if ln.strip()]
    assert len(lines) == 1
    assert json.loads(lines[0])["name"] == "f::bench"
    rep.finalize()


def test_fanout_finalize_runs_every_sink_despite_failure():
    """One sink failing to finalize (e.g. full disk) must not skip the others."""
    from mew.reporter import Fanout

    calls: list[str] = []

    class Ok:
        def __init__(self, tag: str) -> None:
            self.tag = tag

        def report_context(self, context) -> None:
            pass

        def report_runs(self, runs) -> None:
            pass

        def finalize(self) -> None:
            calls.append(self.tag)

    class Boom(Ok):
        def finalize(self) -> None:
            super().finalize()
            raise RuntimeError("disk full")

    fanout = Fanout([Boom("boom"), Ok("ok")])
    with pytest.raises(RuntimeError, match="disk full"):
        fanout.finalize()
    assert calls == ["boom", "ok"]


def test_rows_carry_the_addressable_benchmark_name(tmp_path):
    @mew.parametrize([{"n": 1}, {"n": 2}], min_time=0.001, repetitions=2)
    def bench_fam(state, n):
        for _ in state:
            pass

    out = tmp_path / "o.json"
    mew.run(min_time="1x", reporter=JSONReporter(output=out))
    rows = json.loads(out.read_text())["benchmarks"]
    (entry,) = mew.REGISTRY.all()
    # Repetition and aggregate rows of one case share the stamp; `name` differs.
    by_case = {row["benchmark"] for row in rows}
    assert by_case == {f"{entry.name}[n=1]", f"{entry.name}[n=2]"}


def test_set_label_in_a_family_does_not_rename_its_cases():
    @mew.parametrize([{"n": 1}, {"n": 2}], iterations=1)
    def bench_fam(state, n):
        state.set_label("same for every case")
        for _ in state:
            pass

    cap = Capture()
    mew.run(reporter=cap)
    (entry,) = mew.REGISTRY.all()
    assert [r["benchmark"] for r in cap.runs] == [f"{entry.name}[n=1]", f"{entry.name}[n=2]"]
    assert {r["label"] for r in cap.runs} == {"same for every case"}


def test_canonical_row_name_appends_the_aggregate_suffix():
    from mew.reporter import canonical_row_name

    row = {"name": "b.py::f/case:0/threads:2", "benchmark": "b.py::f[n=10]/threads:2"}
    assert canonical_row_name(row) == "b.py::f[n=10]/threads:2"
    assert canonical_row_name({**row, "aggregate_name": "mean"}) == "b.py::f[n=10]/threads:2_mean"


def test_rows_serialize_time_units_as_plain_strings(tmp_path):
    """BenchmarkResult carries strings, not bound enums: a leaked `Run.time_unit` would be
    archived as "TimeUnit.ns"."""

    @mew.benchmark(unit="us")
    def bench_units(state):
        for _ in state:
            pass

    out = tmp_path / "out.json"
    mew.run(min_time="1x", repetitions=2, reporter=JSONReporter(output=out))
    for bench in json.loads(out.read_text())["benchmarks"]:
        assert bench["time_unit"] == "us"
        assert bench["run_type"] in ("iteration", "aggregate")


def test_cv_aggregate_is_reported_as_a_ratio(tmp_path):
    """GB's `cv` aggregate is a ratio: scaling it by the time unit and dividing
    by the repetition count, as for time aggregates, reports garbage."""

    @mew.benchmark(unit="us", iterations=100)
    def bench_cv(state):
        for _ in state:
            sum(range(50))

    out = tmp_path / "out.json"
    mew.run(repetitions=4, reporter=JSONReporter(output=out))
    rows = json.loads(out.read_text())["benchmarks"]
    aggregates = {r["aggregate_name"]: r for r in rows if r["aggregate_name"]}
    assert all("aggregate_unit" not in r for r in rows if not r["aggregate_name"])
    assert aggregates["mean"]["aggregate_unit"] == "time"
    cv = aggregates["cv"]
    assert cv["aggregate_unit"] == "percentage"
    stddev, mean = aggregates["stddev"], aggregates["mean"]
    assert cv["real_time"] == pytest.approx(stddev["real_time"] / mean["real_time"])

    from mew._console import Terminal

    buf = io.StringIO()
    rep = RichReporter(terminal=Terminal(file=buf, width=200, color=False))
    rep.report_context({"session": {"host": "h"}, "context": {}})
    rep.report_runs([cv])
    line = buf.getvalue().splitlines()[-1]
    assert f"{cv['real_time']:.2%}" in line
    assert " us" not in line


@pytest.mark.parametrize(
    ("value", "one_k", "exponent", "expected"),
    [
        (42, 1000, False, "42"),
        (1500, 1000, False, "1.5k"),
        (1e6, 1000, False, "1M"),
        (1288490188.8, 1024, False, "1.2Gi"),
        (0.5, 1000, False, "0.5"),
        (0.005, 1000, False, "5m"),
        (-1500, 1000, False, "-1.5k"),
        (3.5e12, 1000, True, "3.5e12"),
        (1024**2, 1024, True, "1*2^20"),
    ],
)
def test_human_readable_matches_google_benchmark(value, one_k, exponent, expected):
    """Expected strings are Google Benchmark's own `HumanReadableNumber` output."""
    from mew.reporter import _human_readable

    assert _human_readable(value, one_k, exponent) == expected


def test_counters_carry_a_format_only_when_they_need_one():
    from mew._core import CounterFlags, CounterOneK

    @mew.benchmark(iterations=10)
    def bench_counters(state):
        for _ in state:
            pass
        state.set_bytes_processed(10 * 1024**3)
        state.set_counter("hits", 42)
        state.set_counter("latency", 10, flags=CounterFlags.kIsRate | CounterFlags.kInvert)
        state.set_counter("big", 3.5e12, flags=CounterFlags.kStringifyExponent)
        state.set_counter("mem", 2**20, one_k=CounterOneK.kIs1024)

    cap = Capture()
    mew.run(reporter=cap)
    (row,) = cap.runs
    assert [{k: v for k, v in c.items() if k != "value"} for c in row["counters"]] == [
        {"name": "big", "format": {"exponent": True}},
        {"name": "bytes_per_second", "format": {"one_k": 1024, "rate": True}},
        {"name": "hits"},
        {"name": "latency", "format": {"rate": True, "inverted": True}},
        {"name": "mem", "format": {"one_k": 1024}},
    ]


def test_rich_reporter_shows_counters_like_google_benchmark():
    from mew._console import Terminal

    row = cast(
        "BenchmarkResult",
        {
            **_fake_row("a::counted"),
            "counters": [
                {
                    "name": "bytes_per_second",
                    "value": 1288490188.8,
                    "format": {"one_k": 1024, "rate": True},
                },
                {"name": "hits", "value": 42.0},
                {"name": "latency", "value": 2e-7, "format": {"rate": True, "inverted": True}},
            ],
        },
    )
    cv = cast(
        "BenchmarkResult",
        {
            **row,
            "aggregate_name": "cv",
            "aggregate_unit": "percentage",
            "counters": [{"name": "hits", "value": 0.05}],
        },
    )
    buf = io.StringIO()
    rep = RichReporter(terminal=Terminal(file=buf, width=200, color=False))
    rep.report_context({"session": {"host": "h"}, "context": {}})
    rep.report_runs([row, cv])
    plain, ratio = buf.getvalue().splitlines()[-3::2]
    assert plain == "  bytes_per_second=1.2Gi/s · hits=42 · latency=200ns"
    assert ratio == "  hits=5.00%"

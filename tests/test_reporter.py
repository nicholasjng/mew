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


def test_rich_reporter_profile_flags_add_columns():
    from mew._console import Terminal

    buf = io.StringIO()
    rep = RichReporter(
        terminal=Terminal(file=buf, width=200, color=False),
        show_memory=True,
        show_cpu=True,
    )
    rep.report_context(
        {"session": {"host": "h"}, "context": {"num_cpus": 1, "cpu_scaling_enabled": False}}
    )
    out = buf.getvalue()
    assert "Peak Mem" in out
    assert "Samples" in out
    assert "Hottest Frame" in out

    # The header alone never exercises row formatting (`_fmt_bytes`, the "-"
    # fallback), so feed real rows too.
    row = _fake_row("bench.py::bench_x")
    row["memory"] = {"peak_bytes": 2 * (1 << 20)}  # 2.0 MB
    row["cpu_profile"] = {
        "sample_count": 42,
        "top_function": {"function": "hot_fn", "file": "mod.py", "line": 10, "self_time": 0.1},
    }
    rep.report_runs([row])
    out = buf.getvalue()
    assert "2.0 MB" in out
    assert "42" in out
    assert "42.0" not in out  # sample_count renders as an integer
    assert "hot_fn (mod.py:10)" in out

    rep.report_runs([_fake_row("bench.py::bench_y")])
    cells = buf.getvalue().splitlines()[-1].split(" │ ")
    # name, iters, real, cpu, [peak, samples, hottest_frame]
    peak, samples, hottest_frame = cells[-3:]
    assert peak.strip() == "-"
    assert samples.strip() == "-"
    assert hottest_frame.strip() == "-"


def test_rich_reporter_left_ellipsizes_long_names():
    from mew._console import Terminal

    name = "benchmarks/some/deeply/nested/path/bench_module/bench_the_actual_function"

    @mew.benchmark(name=name)
    def bench_x(state):
        for _ in state:
            pass

    buf = io.StringIO()
    # Narrow width forces truncation; the meaningful function suffix must survive.
    rep = RichReporter(terminal=Terminal(file=buf, width=60, color=False))
    mew.run(min_time="1x", reporter=rep)
    out = buf.getvalue()
    assert "…" in out
    assert "bench_the_actual_function" in out


def test_rich_reporter_right_ellipsizes_overlong_label_and_hottest_frame():
    """Unlike the name column, the fixed-width label and hottest-frame columns
    keep their left prefix."""
    from mew._console import Terminal

    buf = io.StringIO()
    rep = RichReporter(
        terminal=Terminal(file=buf, width=200, color=False),
        show_label=True,
        show_cpu=True,
    )
    rep.report_context(
        {"session": {"host": "h"}, "context": {"num_cpus": 1, "cpu_scaling_enabled": False}}
    )

    row = _fake_row("bench.py::bench_x", label="a-very-long-case-label-well-past-twenty-chars")
    row["cpu_profile"] = {
        "sample_count": 1,
        "top_function": {
            "function": "a_very_long_function_name_that_exceeds_thirty_characters",
            "file": "mod.py",
            "line": 1,
            "self_time": 0.1,
        },
    }
    rep.report_runs([row])
    out = buf.getvalue()

    assert "Label" in out
    # Fixed widths from `RichReporter.report_context`: Label=20, Hottest Frame=30.
    assert "a-very-long-case-la…" in out  # left prefix of label survives, truncated
    assert "a_very_long_function_name_tha…" in out  # left prefix of hottest frame


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
    from mew._console import Terminal

    buf = io.StringIO()
    rep = RichReporter(terminal=Terminal(file=buf, width=200, color=False), show_cpu=True)
    rep.report_context({"session": {"host": "h"}, "context": {}})
    row = _fake_row("bench.py::bench_x")
    row["cpu_profile"] = {"sample_count": 1, "top_function": top}
    rep.report_runs([row])
    assert buf.getvalue().splitlines()[-1].split(" │ ")[-1].strip() == shown


def test_rich_reporter_instruction_column_keeps_the_shares():
    from mew._console import Terminal

    buf = io.StringIO()
    rep = RichReporter(
        terminal=Terminal(file=buf, width=200, color=False), show_cpu=True, show_instructions=True
    )
    rep.report_context(
        {"session": {"host": "h"}, "context": {"num_cpus": 1, "cpu_scaling_enabled": False}}
    )
    assert "Hottest Instruction" in buf.getvalue()

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
    rep.report_runs([_fake_row("bench.py::bench_y")])

    cells = [line.split(" │ ")[-1].rstrip() for line in buf.getvalue().splitlines()[-4:]]
    assert cells == [
        "BINARY_OP 62% · 62% generic",
        "LOAD_ATTR_INSTANCE_VALUE 32%",
        # Width 30: the name is cut, the shares are not.
        "CALL_METHOD… 40% · 10% generic",
        "-",
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
    plain, ratio = buf.getvalue().splitlines()[-2:]
    assert plain.endswith(" │ bytes_per_second=1.2Gi/s hits=42 latency=200ns")
    assert ratio.endswith(" │ hits=5.00%")

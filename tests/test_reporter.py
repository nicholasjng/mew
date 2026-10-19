"""Reporters: JSON/JSONL row shape and streaming, and the RichReporter table."""

from __future__ import annotations

import io
import json
from unittest import mock

import pytest
from _helpers import Capture

import mew
from mew import BenchmarkResult
from mew._console import Terminal
from mew.compare import read_results
from mew.reporter import Fanout, JSONLReporter, JSONReporter, RichReporter, _human_readable


def _run_one(reporter):
    @mew.benchmark(unit="us")
    def bench_x(state):
        for _ in state:
            pass

    mew.run(min_time="1x", reporter=reporter)


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


def _feed(reporter, *rows: BenchmarkResult) -> None:
    """Drive a reporter through one run of ``rows``, without running anything."""
    reporter.report_context({"session": {"host": "h"}, "context": {}})
    reporter.report_runs(list(rows))
    reporter.finalize()


def test_json_reporter_writes_the_documented_shape(tmp_path):
    out = tmp_path / "results.json"
    _run_one(JSONReporter(output=out))

    doc = json.loads(out.read_text())
    assert {"session", "context", "benchmarks"} <= doc.keys()
    (bench,) = doc["benchmarks"]
    assert {"name", "benchmark", "iterations", "real_time", "cpu_time", "threads"} <= bench.keys()
    assert {"session", "context"} <= bench.keys()  # every row stands alone
    # Plain strings, not bound enums: a leaked `Run.time_unit` would be archived as "TimeUnit.us".
    assert (bench["time_unit"], bench["run_type"]) == ("us", "iteration")


def test_json_reporter_leaves_a_stream_open():
    buf = io.StringIO()
    _feed(JSONReporter(output=buf), _fake_row("a::one"))
    assert json.loads(buf.getvalue())["benchmarks"][0]["name"] == "a::one"


@pytest.mark.parametrize(
    ("reporter", "suffix"), [(JSONReporter, ".json"), (JSONLReporter, ".jsonl")]
)
def test_file_reporter_can_be_reused(tmp_path, reporter, suffix):
    # A second run reopens the sink finalize() closed, and starts it over.
    out = tmp_path / f"results{suffix}"
    rep = reporter(output=out)
    for _ in range(2):
        _feed(rep, _fake_row("a::one"))
        assert len(read_results(out)) == 1


@pytest.mark.parametrize("reporter", [JSONReporter, JSONLReporter])
def test_file_reporters_write_each_row_as_it_arrives(tmp_path, reporter):
    # On disk before the run ends, not buffered; JSONL is the format that survives an interruption.
    out = tmp_path / "results"
    rep = reporter(output=out)
    rep.report_context({})
    rep.report_runs([_fake_row("a::one")])
    assert "a::one" in out.read_text()
    rep.finalize()


@pytest.mark.parametrize("suffix", [".jsonl", ".jsonl.gz"])
def test_jsonl_archive_is_queryable_by_duckdb(tmp_path, suffix):
    # The archive contract: self-contained rows, gzipped or not, that DuckDB
    # queries directly, with nested blocks (`context`, `session`) as structs.
    duckdb = pytest.importorskip("duckdb")
    mew.set_context("dataset.size", 1024)
    out = tmp_path / f"results{suffix}"
    _run_one(JSONLReporter(output=out))

    query = f"SELECT benchmark, context.dataset.size, session.id FROM '{out}'"
    ((name, size, session_id),) = duckdb.connect().execute(query).fetchall()
    assert name == mew.REGISTRY.all()[0].name
    assert size == 1024
    assert session_id  # identity on the row itself, no header join


def test_jsonl_reporter_gzips_an_uppercase_gz_suffix(tmp_path):
    # The CLI and the reader match `.jsonl.gz` in any case, so the writer must too.
    out = tmp_path / "results.JSONL.GZ"
    _feed(JSONLReporter(output=out), _fake_row("a::one"))
    assert out.read_bytes()[:2] == b"\x1f\x8b"  # the gzip magic number
    assert [row["name"] for row in read_results(out)] == ["a::one"]


def _rich(width: int = 200, **flags) -> tuple[io.StringIO, RichReporter]:
    buf = io.StringIO()
    rep = RichReporter(terminal=Terminal(file=buf, width=width, color=False), **flags)
    rep.report_context(
        {"session": {"host": "h"}, "context": {"num_cpus": 4, "cpu_scaling_enabled": False}}
    )
    return buf, rep


def test_rich_reporter_prints_banner_and_header_before_the_first_row():
    buf, _ = _rich()
    banner, header, _rule = buf.getvalue().splitlines()
    assert banner == "mew · host=h cpus=4 scaling=disabled"
    assert [cell.strip() for cell in header.split(" │ ")] == ["Benchmark", "Iters", "Real", "CPU"]


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
    unsampled = _fake_row("bench.py::bench_y")
    unsampled["cpu_profile"] = {"sample_count": 0, "top_function": None}
    rep.report_runs([row, unsampled, _fake_row("bench.py::bench_z")])
    lines = buf.getvalue().splitlines()[-6:]
    assert lines[0].split(" │ ")[-1].strip() == "2.0 MB"
    assert lines[1] == "  4,200 samples · hottest hot_fn (mod.py:10)"
    # Rows without a figure say so rather than leaving a gap.
    assert lines[2].split(" │ ")[-1].strip() == "-"
    assert lines[3] == "  0 samples"
    assert lines[5] == "  no profile"


def test_rich_reporter_lists_what_has_no_column_below_the_row():
    """A label the body set, the counters and the profile go below the row,
    filled to the terminal width without splitting a part."""
    buf, rep = _rich(width=64, show_cpu=True)
    row = _fake_row("bench.py::bench_x", label="warm-cache")
    row["counters"] = [{"name": "items_per_second", "value": 2.5e8, "format": {"rate": True}}]
    row["cpu_profile"] = {"sample_count": 1, "top_function": "work (bench.py:6)"}
    rep.report_runs([row])
    assert buf.getvalue().splitlines()[-2:] == [
        "  warm-cache · items_per_second=250M/s · 1 sample",
        "    hottest work (bench.py:6)",
    ]


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


@pytest.mark.parametrize(
    ("generic", "shown"), [(0.62, "BINARY_OP 62%, 62% generic"), (0.0, "BINARY_OP 62%")]
)
def test_rich_reporter_names_the_hottest_instruction(generic, shown):
    buf, rep = _rich(show_cpu=True)
    row = _fake_row("bench.py::bench_x")
    row["cpu_profile"] = {
        "sample_count": 1,
        # Only the first, hottest entry reaches the console.
        "top_instructions": [
            {"instruction": "BINARY_OP", "share": 0.62},
            {"instruction": "STORE_FAST", "share": 0.01},
        ],
        "unspecialized_share": generic,
    }
    rep.report_runs([row])
    assert buf.getvalue().splitlines()[-1] == f"  1 sample · {shown}"


def test_rich_reporter_names_a_row_by_its_benchmark_field():
    """The `name[label]` form, as `mew compare` shows it, not GB's raw
    `/case:N/min_time:…` name; the case's label isn't repeated below it."""
    buf, rep = _rich()
    raw = "bench.py::bench_x/case:0/min_time:0.200"
    rep.report_runs([_fake_row(raw, label="small", benchmark="bench.py::bench_x[small]")])
    assert buf.getvalue().splitlines()[-1].split(" │ ")[0].rstrip() == "bench.py::bench_x[small]"


def test_fanout_finalizes_every_sink_in_order_despite_a_failure():
    """One sink failing to finalize (e.g. full disk) must not skip the others."""
    sinks = mock.Mock()
    sinks.boom.finalize.side_effect = RuntimeError("disk full")
    with pytest.raises(RuntimeError, match="disk full"):
        Fanout([sinks.boom, sinks.ok]).finalize()
    assert sinks.method_calls == [mock.call.boom.finalize(), mock.call.ok.finalize()]


def test_family_rows_are_named_by_their_case_not_their_label():
    # `benchmark` is the addressable `name[label]` (repetitions share it), and
    # `set_label()` only sets `label`: cases that set the same one stay apart.
    @mew.parametrize([{"n": 1}, {"n": 2}], iterations=1, repetitions=2)
    def bench_fam(state, n):
        state.set_label("same for every case")
        for _ in state:
            pass

    cap = Capture()
    mew.run(reporter=cap)
    (entry,) = mew.REGISTRY.all()
    assert [r["benchmark"] for r in cap.runs] == 2 * [f"{entry.name}[n=1]"] + 2 * [
        f"{entry.name}[n=2]"
    ]
    assert {r["label"] for r in cap.runs} == {"same for every case"}


def test_repetitions_report_only_their_own_rows(tmp_path):
    """mew reduces repetitions itself: files keep each repetition, and none of
    Google Benchmark's aggregate rows; the console sums them up at the end."""

    @mew.benchmark(iterations=100)
    def bench_rep(state):
        for _ in state:
            pass

    out, buf = tmp_path / "out.json", io.StringIO()
    console = RichReporter(terminal=Terminal(file=buf, width=200, color=False))
    mew.run(repetitions=3, reporter=[JSONReporter(output=out), console])
    rows = json.loads(out.read_text())["benchmarks"]
    assert [r["repetition_index"] for r in rows] == [0, 1, 2]
    assert {(r["run_type"], r["aggregate_name"]) for r in rows} == {("iteration", "")}
    lines = buf.getvalue().splitlines()
    summary = lines.index("Median of repetitions")
    assert sum("bench_rep" in line for line in lines[:summary]) == 3
    assert [cell.strip() for cell in lines[-1].split(" │ ")][:2] == [rows[0]["benchmark"], "3"]


def _repetitions(name: str, reals: tuple[float, ...], family_index: int) -> list[BenchmarkResult]:
    runs = []
    for i, real in enumerate(reals):
        row = _fake_row(name)
        row.update(real_time=real, repetition_index=i, repetitions=len(reals))
        row.update(family_index=family_index)
        runs.append(row)
    return runs


def test_rich_reporter_sums_up_repetitions_at_the_end_like_mew_compare():
    buf, rep = _rich()
    # Random interleaving reports benchmarks as they finish: here b before a.
    rep.report_runs(_repetitions("bench.py::bench_b", (20.0, 30.0), family_index=1))
    rep.report_runs([_fake_row("bench.py::bench_once")])  # one run: nothing to sum up
    rep.report_runs(_repetitions("bench.py::bench_a", (10.0, 11.0, 15.0), family_index=0))
    assert "Median of repetitions" not in buf.getvalue()
    rep.finalize()

    lines = buf.getvalue().splitlines()
    assert lines[-6:-4] == ["", "Median of repetitions"]
    rows = [[cell.strip() for cell in line.split(" │ ")] for line in lines[-2:]]
    # In registration order; the median times, the real time's stddev and stddev / mean.
    assert rows == [
        ["bench.py::bench_a", "3", "11.00 ns", "2.65 ns", "22.05%", "1.00 ns"],
        ["bench.py::bench_b", "2", "25.00 ns", "7.07 ns", "28.28%", "1.00 ns"],
    ]


def test_rich_reporter_can_leave_repetitions_to_the_summary():
    buf, rep = _rich(show_repetitions=False)
    # No header yet: every row may end up in the summary, leaving an empty table.
    assert "Benchmark" not in buf.getvalue()
    rep.report_runs(_repetitions("bench.py::bench_a", (10.0, 14.0, 12.0), family_index=0))
    assert "bench_a" not in buf.getvalue()
    rep.report_runs([_fake_row("bench.py::bench_once")])  # a single run still streams
    rep.finalize()
    lines = buf.getvalue().splitlines()
    assert lines[1].startswith("Benchmark") and lines[3].startswith("bench.py::bench_once")
    assert [line for line in lines if "bench_a" in line] == [lines[-1]]  # its summary only


def test_rich_reporter_reads_the_terminal_width_once_per_run(monkeypatch):
    # Piped, the width comes from /dev/tty, which isn't worth opening for every row.
    probes = []
    monkeypatch.setattr("mew._console._terminal_width", lambda: probes.append(1) or 60)
    rep = RichReporter(terminal=Terminal(file=io.StringIO(), color=False))
    rep.report_context({})
    rep.report_runs(_repetitions("bench.py::bench_a", (10.0, 11.0), family_index=0))
    rep.finalize()
    assert len(probes) == 1


def test_rich_reporter_prints_no_summary_without_repetitions():
    buf, rep = _rich()
    rep.report_runs([_fake_row("bench.py::bench_x")])
    before = buf.getvalue()
    rep.finalize()
    assert buf.getvalue() == before


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
    buf, rep = _rich()
    row = _fake_row("a::counted")
    row["counters"] = [
        {
            "name": "bytes_per_second",
            "value": 1288490188.8,
            "format": {"one_k": 1024, "rate": True},
        },
        {"name": "hits", "value": 42.0},
        {"name": "latency", "value": 2e-7, "format": {"rate": True, "inverted": True}},
    ]
    rep.report_runs([row])
    assert buf.getvalue().splitlines()[-1] == "  bytes_per_second=1.2Gi/s · hits=42 · latency=200ns"

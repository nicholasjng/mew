"""Reporter output shape (JSON schema, RichReporter doesn't crash)."""

from __future__ import annotations

import io
import json

import pytest

import mew
from mew import BenchmarkResult
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


def _fake_row(name: str, label: str = "") -> BenchmarkResult:
    """A minimal BenchmarkResult dict, the shape reporters now consume directly."""
    return {
        "name": name,
        "run_name": name,
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
        "counters": {},
    }


def test_json_reporter_streams_forward_only(tmp_path):
    """GB-style: rows land on disk as they arrive, the closer only at finalize.

    The document parses only after finalize; JSONL is the interruption-safe format.
    """
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
    assert doc["context"]["session"]["host"] == "h"
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
    assert real_time > 0
    assert sz == 1024
    assert session_id  # identity on the row itself, no header join


def test_jsonl_gz_reporter_duckdb_query_round_trip(tmp_path):
    duckdb = pytest.importorskip("duckdb")

    out = tmp_path / "results.jsonl.gz"
    _run_one(JSONLReporter(output=out))

    (row,) = duckdb.connect().execute(f"SELECT name FROM read_json_auto('{out}')").fetchall()
    assert "bench_x" in row[0]


def test_rich_reporter_runs_without_error():
    # Use an in-memory console so the test doesn't paint the terminal.
    from mew._console import Terminal

    buf = io.StringIO()
    rep = RichReporter(terminal=Terminal(file=buf, width=120, color=False))
    _run_one(rep)
    out = buf.getvalue()
    assert "host=" in out
    assert "Benchmark" in out  # the table header


def test_rich_reporter_streams_header_before_first_run():
    """Header must be emitted at report_context, not deferred to finalize."""
    import io

    from mew._console import Terminal
    from mew.reporter import RichReporter

    buf = io.StringIO()
    rep = RichReporter(terminal=Terminal(file=buf, width=120, color=False))
    rep.report_context({"host_name": "h", "num_cpus": 4, "cpu_scaling": "off"})
    # Header is already on screen — we haven't reported any runs yet.
    out = buf.getvalue()
    assert "host=" in out
    assert "Benchmark" in out
    assert "Iters" in out


def test_rich_reporter_profile_flags_add_columns():
    import io

    from mew._console import Terminal
    from mew.reporter import RichReporter

    buf = io.StringIO()
    rep = RichReporter(
        terminal=Terminal(file=buf, width=200, color=False),
        show_memory=True,
        show_cpu=True,
    )
    rep.report_context({"host_name": "h", "num_cpus": 1, "cpu_scaling": "?"})
    out = buf.getvalue()
    assert "Peak Mem" in out
    assert "Samples" in out
    assert "Hottest Frame" in out

    # Header alone doesn't invoke the row-formatting code at all — a bug in
    # `_fmt_bytes`'s thresholds or the memory/cpu `None -> "-"` fallback would
    # slip past the assertions above. Feed a real data row to catch that.
    row = _fake_row("bench.py::bench_x")
    row["memory"] = {"peak_bytes": 2 * (1 << 20)}  # 2.0 MB
    row["cpu_profile"] = {"sample_count": 42, "top_function": "hot_fn (mod.py:10)"}
    rep.report_runs([row])
    out = buf.getvalue()
    assert "2.0 MB" in out
    assert "42" in out
    assert "hot_fn (mod.py:10)" in out

    # A row with neither profile attached must fall back to "-", not crash.
    rep.report_runs([_fake_row("bench.py::bench_y")])
    cells = buf.getvalue().splitlines()[-1].split(" │ ")
    # name, iters, real, cpu, [peak, samples, hottest_frame]
    peak, samples, hottest_frame = cells[-3:]
    assert peak.strip() == "-"
    assert samples.strip() == "-"
    assert hottest_frame.strip() == "-"


def test_rich_reporter_shows_label_column_for_families():
    from mew._console import Terminal

    @mew.parametrize([{"n": 10}, {"n": 20}], ids=["small", "big"])
    def bench_x(state, n):
        for _ in state:
            pass

    buf = io.StringIO()
    rep = RichReporter(terminal=Terminal(file=buf, width=120, color=False), show_label=True)
    mew.run(min_time="1x", reporter=rep)
    out = buf.getvalue()
    assert "Label" in out  # header
    assert "small" in out and "big" in out  # case labels rendered per row


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
    """Mirrors the name column's left-ellipsis test, but for the fixed-width
    columns added by `show_label` and `--sample`, which use `_truncate_right`
    instead. Regressed by df346b6 if this drifts back to inline slicing."""
    from mew._console import Terminal

    buf = io.StringIO()
    rep = RichReporter(
        terminal=Terminal(file=buf, width=200, color=False),
        show_label=True,
        show_cpu=True,
    )
    rep.report_context({"host_name": "h", "num_cpus": 1, "cpu_scaling": "?"})

    row = _fake_row("bench.py::bench_x", label="a-very-long-case-label-well-past-twenty-chars")
    row["cpu_profile"] = {
        "sample_count": 1,
        "top_function": "a_very_long_function_name_that_exceeds_thirty_characters (mod.py:1)",
    }
    rep.report_runs([row])
    out = buf.getvalue()

    # Fixed widths from `_compute_widths`: label=20, hottest_frame=30.
    assert "…" in out
    assert "a-very-long-case-la…" in out  # left prefix of label survives, truncated
    assert "a_very_long_function_name_tha" in out  # left prefix of hottest frame


def test_rich_reporter_renders_canonical_name():
    """The live table shows the human `name[label]` form, not GB's raw
    `/case:N/min_time:…` suffixes, so it reads the same as `mew compare`."""
    from mew._console import Terminal

    buf = io.StringIO()
    rep = RichReporter(terminal=Terminal(file=buf, width=120, color=False))
    rep.report_context({"host_name": "h", "num_cpus": 4, "cpu_scaling": "off"})
    rep.report_runs([_fake_row("bench.py::bench_x/case:0/min_time:0.200", label="small")])
    out = buf.getvalue()
    assert "bench.py::bench_x[small]" in out
    assert "case:0" not in out
    assert "min_time" not in out


def test_jsonl_reporter_streams_one_object_per_line(tmp_path):
    from mew.reporter import JSONLReporter

    @mew.parametrize([{"n": 1}, {"n": 2}])
    def bench_x(state, n):
        for _ in state:
            pass

    out = tmp_path / "results.jsonl"
    mew.run(min_time="1x", reporter=JSONLReporter(output=out))

    lines = [ln for ln in out.read_text().splitlines() if ln.strip()]
    rows = [json.loads(ln) for ln in lines]  # each line is independently valid JSON
    # Pure NDJSON: no context header, every line is a self-contained row.
    assert len(rows) == 2
    assert all("/case:" in r["name"] for r in rows)
    assert all(r["session"]["host"] and r["session"]["date"] for r in rows)


def test_jsonl_reporter_flushes_incrementally(tmp_path):
    """Rows are on disk after report_runs, not buffered until finalize."""
    from mew.reporter import JSONLReporter

    out = tmp_path / "partial.jsonl"
    rep = JSONLReporter(output=out)
    rep.report_context({})

    rep.report_runs([_fake_row("f::bench")])
    # File already holds the row before finalize() is called.
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
    # The failing sink ran first (call order preserved), the healthy one still ran.
    assert calls == ["boom", "ok"]


def test_canonical_name_keeps_the_aggregate_suffix():
    """GB appends `_mean`/`_median`/... *after* the args part, so `/case:N` is not
    at the end of an aggregate row's name. The label still swaps in, and the
    suffix stays so aggregates remain distinct from the rows they summarize."""
    from mew.reporter import canonical_name

    assert canonical_name("b.py::f/case:0", "n=10") == "b.py::f[n=10]"
    assert canonical_name("b.py::f/case:0_mean", "n=10") == "b.py::f[n=10]_mean"
    assert canonical_name("b.py::f/case:12_stddev", "n=10") == "b.py::f[n=10]_stddev"
    # Option suffixes still go entirely.
    assert canonical_name("b.py::f/case:0/min_time:0.200", "n=10") == "b.py::f[n=10]"
    # Unlabelled and non-family names are untouched.
    assert canonical_name("b.py::f/case:0_mean", "") == "b.py::f/case:0_mean"
    assert canonical_name("b.py::plain", "n=10") == "b.py::plain"

"""Reporter output shape (JSON schema, RichReporter doesn't crash)."""

from __future__ import annotations

import io
import json

import pytest

import mew
from mew import RunRow
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


def _fake_row(name: str, label: str = "") -> RunRow:
    """A minimal RunRow dict, the shape reporters now consume directly."""
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
    rep.report_context({"host_name": "h", "num_cpus": 4})

    rep.report_runs([_fake_row("a::one"), _fake_row("a::two")])
    partial = out.read_text()
    assert "a::one" in partial and "a::two" in partial  # streamed, not buffered
    with pytest.raises(json.JSONDecodeError):
        json.loads(partial)  # open-ended until finalize, like GB itself

    rep.report_runs([_fake_row("a::three")])
    rep.finalize()
    doc = json.loads(out.read_text())
    assert doc["context"]["host_name"] == "h"
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
        f"SELECT name, real_time, custom.dataset.size, session_id FROM '{out}'"
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
    rep.report_context({"host_name": "h", "num_cpus": 4, "mhz_per_cpu": 1000, "cpu_scaling": "off"})
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
    rep.report_context({"host_name": "h", "num_cpus": 1, "mhz_per_cpu": 1000, "cpu_scaling": "?"})
    out = buf.getvalue()
    assert "Peak Mem" in out
    assert "Samples" in out
    assert "Hottest Frame" in out


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

    name = "benchmarks/some/deeply/nested/path/bench_module.py::bench_the_actual_function"

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


def test_rich_reporter_renders_canonical_name():
    """The live table shows the human `name[label]` form, not GB's raw
    `/case:N/min_time:…` suffixes, so it reads the same as `mew compare`."""
    from mew._console import Terminal

    buf = io.StringIO()
    rep = RichReporter(terminal=Terminal(file=buf, width=120, color=False))
    rep.report_context({"host_name": "h", "num_cpus": 4, "mhz_per_cpu": 1000, "cpu_scaling": "off"})
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
    assert all(r["host_name"] and r["date"] for r in rows)


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

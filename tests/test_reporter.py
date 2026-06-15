"""Reporter output shape (JSON schema, RichReporter doesn't crash)."""

from __future__ import annotations

import io
import json

import pytest

import mew
from mew.reporter import JSONReporter, RichReporter


def _run_one(reporter):
    @mew.benchmark
    def bench_x(state):
        for _ in state:
            pass

    mew.run(argv=["mew", "--benchmark_min_time=1x"], reporter=reporter)


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


def _fake_run(name: str, label: str = ""):
    class FakeName:
        function_name = name
        args = ""

    class FakeRun:
        run_name = FakeName()
        family_index = 0
        per_family_instance_index = 0
        aggregate_name = ""
        repetitions = 1
        repetition_index = 0
        threads = 1
        iterations = 1
        real_accumulated_time = 0.0
        cpu_accumulated_time = 0.0
        report_label = label
        skipped = False
        skip_message = ""
        counters: dict = {}

        def benchmark_name(self):
            return name

        def adjusted_real_time(self):
            return 1.0

        def adjusted_cpu_time(self):
            return 1.0

        @property
        def run_type(self):
            return mew.RunType.iteration

        @property
        def time_unit(self):
            return mew.TimeUnit.ns

    return FakeRun()


def test_json_reporter_file_is_valid_after_each_flush_without_finalize(tmp_path):
    """The streamed file must parse as a complete document at every flush —
    a Ctrl-C between flushes leaves a usable file, not a dangling `[`."""
    out = tmp_path / "results.json"
    rep = JSONReporter(output=out)
    rep.report_context({"host_name": "h", "num_cpus": 4})

    # After the header alone: valid document, context present, empty benchmarks.
    doc = json.loads(out.read_text())
    assert doc["context"]["host_name"] == "h"
    assert doc["benchmarks"] == []

    rep.report_runs([_fake_run("a::one"), _fake_run("a::two")])
    # Still valid mid-run, before finalize() — this is the Ctrl-C survivability.
    doc = json.loads(out.read_text())
    assert [b["name"] for b in doc["benchmarks"]] == ["a::one", "a::two"]

    rep.report_runs([_fake_run("a::three")])
    doc = json.loads(out.read_text())
    assert [b["name"] for b in doc["benchmarks"]] == ["a::one", "a::two", "a::three"]

    rep.finalize()
    # finalize() doesn't corrupt or duplicate anything.
    doc = json.loads(out.read_text())
    assert len(doc["benchmarks"]) == 3


def test_parquet_reporter_writes_typed_columns(tmp_path):
    pa = pytest.importorskip("pyarrow")
    pq = pytest.importorskip("pyarrow.parquet")

    import mew
    from mew.reporter import ParquetReporter

    mew.set_context("dataset.size", 1024)
    mew.set_context("commit", "abc")

    out = tmp_path / "results.parquet"
    rep = ParquetReporter(output=out)
    _run_one(rep)

    table = pq.read_table(out)
    assert table.num_rows == 1

    schema = table.schema
    # Spot-check the static schema: numeric columns, map<string,double>, custom string.
    assert pa.types.is_float64(schema.field("real_time").type)
    assert pa.types.is_int64(schema.field("iterations").type)
    assert pa.types.is_map(schema.field("counters").type)
    assert pa.types.is_string(schema.field("custom").type)
    assert pa.types.is_timestamp(schema.field("date").type)

    row = table.to_pylist()[0]
    assert "bench_x" in row["name"]
    assert row["iterations"] >= 1
    assert row["time_unit"] == "ns"
    # `custom` is a JSON string; round-trip it.
    assert json.loads(row["custom"]) == {"dataset": {"size": 1024}, "commit": "abc"}


def test_parquet_reporter_omits_custom_when_no_context(tmp_path):
    pytest.importorskip("pyarrow")
    pq = pytest.importorskip("pyarrow.parquet")

    from mew.reporter import ParquetReporter

    out = tmp_path / "results.parquet"
    _run_one(ParquetReporter(output=out))

    row = pq.read_table(out).to_pylist()[0]
    assert row["custom"] is None


def test_parquet_reporter_duckdb_query_round_trip(tmp_path):
    pytest.importorskip("pyarrow")
    duckdb = pytest.importorskip("duckdb")

    import mew
    from mew.reporter import ParquetReporter

    mew.set_context("dataset.size", 1024)

    out = tmp_path / "results.parquet"
    _run_one(ParquetReporter(output=out))

    con = duckdb.connect()
    rows = con.execute(
        "SELECT name, real_time, "
        "CAST(json_extract(custom, '$.dataset.size') AS BIGINT) AS sz "
        f"FROM '{out}'"
    ).fetchall()
    assert len(rows) == 1
    name, real_time, sz = rows[0]
    assert "bench_x" in name
    assert real_time > 0
    assert sz == 1024


def test_rich_reporter_runs_without_error():
    # Use an in-memory console so the test doesn't paint the terminal.
    from rich.console import Console

    buf = io.StringIO()
    rep = RichReporter(console=Console(file=buf, force_terminal=False, width=120))
    _run_one(rep)
    out = buf.getvalue()
    assert "host=" in out
    assert "Benchmark" in out  # the table header


def test_rich_reporter_streams_header_before_first_run():
    """Header must be emitted at report_context, not deferred to finalize."""
    import io

    from rich.console import Console

    from mew.reporter import RichReporter

    buf = io.StringIO()
    rep = RichReporter(console=Console(file=buf, force_terminal=False, width=120))
    rep.report_context({"host_name": "h", "num_cpus": 4, "mhz_per_cpu": 1000, "cpu_scaling": "off"})
    # Header is already on screen — we haven't reported any runs yet.
    out = buf.getvalue()
    assert "host=" in out
    assert "Benchmark" in out
    assert "Iters" in out


def test_rich_reporter_profile_flags_add_columns():
    import io

    from rich.console import Console

    from mew.reporter import RichReporter

    buf = io.StringIO()
    rep = RichReporter(
        console=Console(file=buf, force_terminal=False, width=200),
        show_memory=True,
        show_cpu=True,
    )
    rep.report_context({"host_name": "h", "num_cpus": 1, "mhz_per_cpu": 1000, "cpu_scaling": "?"})
    out = buf.getvalue()
    assert "Peak Mem" in out
    assert "Total Alloc" in out
    assert "Samples" in out
    assert "Hottest Frame" in out


def test_rich_reporter_shows_label_column_for_families():
    from rich.console import Console

    @mew.parametrize([{"n": 10}, {"n": 20}], ids=["small", "big"])
    def bench_x(state, n):
        for _ in state:
            pass

    buf = io.StringIO()
    rep = RichReporter(console=Console(file=buf, force_terminal=False, width=120), show_label=True)
    mew.run(argv=["mew", "--benchmark_min_time=1x"], reporter=rep)
    out = buf.getvalue()
    assert "Label" in out  # header
    assert "small" in out and "big" in out  # case labels rendered per row


def test_rich_reporter_left_ellipsizes_long_names():
    from rich.console import Console

    name = "benchmarks/some/deeply/nested/path/bench_module.py::bench_the_actual_function"

    @mew.benchmark(name=name)
    def bench_x(state):
        for _ in state:
            pass

    buf = io.StringIO()
    # Narrow width forces truncation; the meaningful function suffix must survive.
    rep = RichReporter(console=Console(file=buf, force_terminal=False, width=60))
    mew.run(argv=["mew", "--benchmark_min_time=1x"], reporter=rep)
    out = buf.getvalue()
    assert "…" in out
    assert "bench_the_actual_function" in out


def test_rich_reporter_renders_canonical_name():
    """The live table shows the human `name[label]` form, not GB's raw
    `/case:N/min_time:…` suffixes, so it reads the same as `mew compare`."""
    from rich.console import Console

    buf = io.StringIO()
    rep = RichReporter(console=Console(file=buf, force_terminal=False, width=120))
    rep.report_context({"host_name": "h", "num_cpus": 4, "mhz_per_cpu": 1000, "cpu_scaling": "off"})
    rep.report_runs([_fake_run("bench.py::bench_x/case:0/min_time:0.200", label="small")])
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
    mew.run(argv=["mew", "--benchmark_min_time=1x"], reporter=JSONLReporter(output=out))

    lines = [ln for ln in out.read_text().splitlines() if ln.strip()]
    docs = [json.loads(ln) for ln in lines]  # each line is independently valid JSON
    # Line 1 is the context header; the rest are benchmark rows.
    assert "context" in docs[0]
    rows = docs[1:]
    assert len(rows) == 2
    assert all("/case:" in r["name"] for r in rows)


def test_jsonl_reporter_flushes_incrementally(tmp_path):
    """Rows are on disk after report_runs, not buffered until finalize."""
    from mew.reporter import JSONLReporter

    out = tmp_path / "partial.jsonl"
    rep = JSONLReporter(output=out)
    rep.report_context({})

    rep.report_runs([_fake_run("f::bench")])
    # File already holds the context header + the row before finalize() is called.
    lines = [ln for ln in out.read_text().splitlines() if ln.strip()]
    assert len(lines) == 2
    assert "context" in json.loads(lines[0])
    assert json.loads(lines[1])["name"] == "f::bench"
    rep.finalize()

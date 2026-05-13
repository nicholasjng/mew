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

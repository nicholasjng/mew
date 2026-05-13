"""CLI sanity: `mew list` and `mew run` via subprocess against a fixture file."""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

FIXTURE = """
    import mew

    @mew.benchmark(tags=("io",))
    def bench_one(state):
        for _ in state:
            pass

    @mew.parametrize([{"n": 1}, {"n": 2}], tags=("cpu",))
    def bench_two(state, n):
        for _ in state:
            pass
"""


@pytest.fixture
def benchdir(tmp_path: Path) -> Path:
    d = tmp_path / "benchmarks"
    d.mkdir()
    (d / "bench_fixture.py").write_text(textwrap.dedent(FIXTURE))
    return d


def _mew(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "mew.cli", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


def test_list_discovers_all_entries(benchdir, tmp_path):
    res = _mew("list", str(benchdir), cwd=tmp_path)
    assert res.returncode == 0, res.stderr
    names = [n for n in res.stdout.splitlines() if n.strip()]
    assert any(n.endswith("::bench_one") for n in names)
    assert sum(1 for n in names if "bench_two[" in n) == 2


def test_list_pattern_filter(benchdir, tmp_path):
    res = _mew("list", str(benchdir), "-k", "bench_one", cwd=tmp_path)
    assert res.returncode == 0
    names = [n for n in res.stdout.splitlines() if n.strip()]
    assert all("bench_one" in n for n in names)
    assert names  # not empty


def test_list_no_matches_exits_nonzero(benchdir, tmp_path):
    res = _mew("list", str(benchdir), "-k", "nonexistent", cwd=tmp_path)
    assert res.returncode == 1


def test_run_json_to_file(benchdir, tmp_path):
    out = tmp_path / "results.json"
    res = _mew(
        "run",
        str(benchdir),
        "--min-time",
        "1x",
        "-o",
        str(out),
        cwd=tmp_path,
    )
    assert res.returncode == 0, res.stderr
    doc = json.loads(out.read_text())
    assert len(doc["benchmarks"]) == 3


def test_run_nodeid_filter(benchdir, tmp_path):
    nodeid = f"{benchdir}/bench_fixture.py::bench_one"
    out = tmp_path / "results.json"
    res = _mew(
        "run",
        nodeid,
        "--min-time",
        "1x",
        "-o",
        str(out),
        cwd=tmp_path,
    )
    assert res.returncode == 0, res.stderr
    doc = json.loads(out.read_text())
    assert len(doc["benchmarks"]) == 1
    assert "bench_one" in doc["benchmarks"][0]["name"]


def test_run_both_sinks(benchdir, tmp_path):
    out = tmp_path / "results.json"
    res = _mew(
        "run",
        str(benchdir),
        "--min-time",
        "1x",
        "-o",
        "stdout",
        "-o",
        str(out),
        cwd=tmp_path,
    )
    assert res.returncode == 0, res.stderr
    # Rich table on stdout AND a JSON file on disk.
    assert "Benchmark" in res.stdout
    doc = json.loads(out.read_text())
    assert len(doc["benchmarks"]) == 3


def test_run_rejects_unknown_output_format(benchdir, tmp_path):
    res = _mew(
        "run",
        str(benchdir),
        "--min-time",
        "1x",
        "-o",
        "results.txt",
        cwd=tmp_path,
    )
    assert res.returncode == 2
    assert "unsupported output format" in res.stderr


def test_list_filter_by_tag(benchdir, tmp_path):
    res = _mew("list", str(benchdir), "-t", "io", cwd=tmp_path)
    assert res.returncode == 0, res.stderr
    names = [n for n in res.stdout.splitlines() if n.strip()]
    assert all("bench_one" in n for n in names)
    assert names


def test_list_filter_by_multiple_tags_is_or(benchdir, tmp_path):
    res = _mew("list", str(benchdir), "-t", "io", "-t", "cpu", cwd=tmp_path)
    assert res.returncode == 0, res.stderr
    names = [n for n in res.stdout.splitlines() if n.strip()]
    # io picks bench_one, cpu picks the two bench_two variants → 3 entries
    assert len(names) == 3


def test_list_show_tags(benchdir, tmp_path):
    res = _mew("list", str(benchdir), "--show-tags", cwd=tmp_path)
    assert res.returncode == 0, res.stderr
    assert "[io]" in res.stdout
    assert "[cpu]" in res.stdout


def test_run_parquet_output(benchdir, tmp_path):
    pytest.importorskip("pyarrow")
    duckdb = pytest.importorskip("duckdb")

    out = tmp_path / "results.parquet"
    res = _mew(
        "run",
        str(benchdir),
        "--min-time",
        "1x",
        "-o",
        str(out),
        cwd=tmp_path,
    )
    assert res.returncode == 0, res.stderr
    rows = duckdb.connect().execute(f"SELECT name FROM '{out}'").fetchall()
    assert len(rows) == 3
    assert all("bench_" in r[0] for r in rows)


def test_run_parquet_pq_extension_accepted(benchdir, tmp_path):
    pytest.importorskip("pyarrow")

    out = tmp_path / "results.pq"
    res = _mew(
        "run",
        str(benchdir),
        "--min-time",
        "1x",
        "-o",
        str(out),
        cwd=tmp_path,
    )
    assert res.returncode == 0, res.stderr
    assert out.exists()


def test_run_filter_by_tag(benchdir, tmp_path):
    out = tmp_path / "results.json"
    res = _mew(
        "run",
        str(benchdir),
        "--min-time",
        "1x",
        "-t",
        "cpu",
        "-o",
        str(out),
        cwd=tmp_path,
    )
    assert res.returncode == 0, res.stderr
    doc = json.loads(out.read_text())
    names = [b["name"] for b in doc["benchmarks"]]
    assert len(names) == 2
    assert all("bench_two" in n for n in names)

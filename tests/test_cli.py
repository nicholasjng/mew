"""CLI sanity: `mew list` and `mew run` via subprocess against a fixture file."""

from __future__ import annotations

import json
import os
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


def _mew(*args: str, cwd: Path, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    return subprocess.run(
        [sys.executable, "-m", "mew.cli", *args],
        cwd=cwd,
        input=stdin,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        check=False,
    )


def test_list_discovers_all_entries(benchdir, tmp_path):
    res = _mew("list", str(benchdir), cwd=tmp_path)
    assert res.returncode == 0, res.stderr
    names = [n for n in res.stdout.splitlines() if n.strip()]
    # Parametrized families list as a single row; per-case expansion happens
    # at run time via Google Benchmark's family bookkeeping.
    assert any(n.endswith("::bench_one") for n in names)
    assert any(n.endswith("::bench_two") for n in names)


def test_list_pattern_filter(benchdir, tmp_path):
    res = _mew("list", str(benchdir), "-k", "bench_one", cwd=tmp_path)
    assert res.returncode == 0
    names = [n for n in res.stdout.splitlines() if n.strip()]
    assert all("bench_one" in n for n in names)
    assert names  # not empty


def test_list_no_matches_exits_nonzero(benchdir, tmp_path):
    res = _mew("list", str(benchdir), "-k", "nonexistent", cwd=tmp_path)
    assert res.returncode == 1


def test_list_pattern_is_regex(benchdir, tmp_path):
    # Alternation matches both fixture benchmarks; anchoring narrows to one.
    res = _mew("list", str(benchdir), "-k", "bench_(one|two)", cwd=tmp_path)
    assert res.returncode == 0, res.stderr
    names = [n for n in res.stdout.splitlines() if n.strip()]
    assert len(names) == 2

    res = _mew("list", str(benchdir), "-k", "bench_one$", cwd=tmp_path)
    names = [n for n in res.stdout.splitlines() if n.strip()]
    assert len(names) == 1 and names[0].endswith("::bench_one")


def test_run_invalid_pattern_errors(benchdir, tmp_path):
    res = _mew("run", str(benchdir), "--min-time", "1x", "-k", "foo(", cwd=tmp_path)
    assert res.returncode == 2
    assert "invalid benchmark filter pattern" in res.stderr


def test_run_k_selects_single_family_case(benchdir, tmp_path):
    # bench_two is parametrized [{n:1},{n:2}]; `n=2` addresses case index 1 only.
    out = tmp_path / "results.json"
    res = _mew("run", str(benchdir), "--min-time", "1x", "-k", "n=2", "-o", str(out), cwd=tmp_path)
    assert res.returncode == 0, res.stderr
    benches = json.loads(out.read_text())["benchmarks"]
    assert len(benches) == 1
    assert "bench_two" in benches[0]["name"]
    assert "/case:1" in benches[0]["name"]


def test_list_show_cases_expands_family(benchdir, tmp_path):
    res = _mew("list", str(benchdir), "--show-cases", cwd=tmp_path)
    assert res.returncode == 0, res.stderr
    names = [n for n in res.stdout.splitlines() if n.strip()]
    # The family expands to one row per case; the plain benchmark stays single.
    assert any(n.endswith("::bench_two[n=1]") for n in names)
    assert any(n.endswith("::bench_two[n=2]") for n in names)
    assert any(n.endswith("::bench_one") for n in names)


def test_run_literal_selects_bracketed_case_without_escaping(benchdir, tmp_path):
    # `-F` lets a pasted `name[label]` select one case; the bare brackets would
    # otherwise be a regex char class and match nothing.
    out = tmp_path / "results.json"
    res = _mew(
        "run",
        str(benchdir),
        "--min-time",
        "1x",
        "-F",
        "-k",
        "bench_two[n=2]",
        "-o",
        str(out),
        cwd=tmp_path,
    )
    assert res.returncode == 0, res.stderr
    benches = json.loads(out.read_text())["benchmarks"]
    assert len(benches) == 1 and "/case:1" in benches[0]["name"]

    # Same pattern without -F: brackets are a char class → no match.
    res = _mew("run", str(benchdir), "--min-time", "1x", "-k", "bench_two[n=2]", cwd=tmp_path)
    assert res.returncode == 1
    assert "no benchmarks found" in res.stderr


def test_list_k_shows_narrowed_family_cases(benchdir, tmp_path):
    res = _mew("list", str(benchdir), "-k", "n=2", cwd=tmp_path)
    assert res.returncode == 0, res.stderr
    names = [n for n in res.stdout.splitlines() if n.strip()]
    # The narrowed case is listed by label; the family and bench_one are gone.
    assert len(names) == 1
    assert names[0].endswith("::bench_two[n=2]")


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
    # io picks bench_one, cpu picks the bench_two family → 2 entries
    # (the family expands to two Runs at run time, but `list` reports families).
    assert len(names) == 2


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


def test_run_benchmark_options_from_pyproject(benchdir, tmp_path):
    # `iterations = 1` matches `--benchmark_min_time=1x`: GB runs each
    # benchmark exactly once. The CLI doesn't expose this directly, so a
    # successful single-iter run proves the config flowed through.
    (tmp_path / "pyproject.toml").write_text('[tool.mew.benchmark_options]\nmin_time = "1x"\n')
    out = tmp_path / "results.json"
    res = _mew("run", str(benchdir), "-o", str(out), cwd=tmp_path)
    assert res.returncode == 0, res.stderr
    doc = json.loads(out.read_text())
    assert len(doc["benchmarks"]) == 3
    assert all(b["iterations"] == 1 for b in doc["benchmarks"])


def test_run_stdin_round_trip(benchdir):
    # `mew list | mew run --stdin` with no xargs; run from the discovery cwd so
    # the relative paths in the listing resolve.
    listing = _mew("list", ".", cwd=benchdir)
    assert listing.returncode == 0, listing.stderr
    res = _mew(
        "run",
        "--stdin",
        "--min-time",
        "1x",
        "--format",
        "jsonl",
        stdin=listing.stdout,
        cwd=benchdir,
    )
    assert res.returncode == 0, res.stderr
    objs = [json.loads(x) for x in res.stdout.splitlines() if x.strip()]
    names = [o["name"] for o in objs if "name" in o]
    assert any("bench_one" in n for n in names)
    assert any("bench_two" in n for n in names)


def test_run_stdin_show_cases_selects_one_case_literally(benchdir):
    # A `name[label]` line from --show-cases is matched literally — no -F, no
    # bracket escaping — so exactly that one case runs.
    listing = _mew("list", ".", "--show-cases", "-k", "n=2", cwd=benchdir)
    assert listing.returncode == 0, listing.stderr
    assert listing.stdout.strip().endswith("bench_two[n=2]")
    res = _mew(
        "run",
        "--stdin",
        "--min-time",
        "1x",
        "--format",
        "jsonl",
        stdin=listing.stdout,
        cwd=benchdir,
    )
    assert res.returncode == 0, res.stderr
    objs = [json.loads(x) for x in res.stdout.splitlines() if x.strip()]
    names = [o["name"] for o in objs if "name" in o]
    assert len(names) == 1 and "/case:1" in names[0]


def test_run_stdin_empty_runs_nothing(benchdir):
    # Empty stdin selects nothing — it does not fall back to benchpaths.
    res = _mew("run", "--stdin", "--min-time", "1x", stdin="", cwd=benchdir)
    assert res.returncode == 1
    assert "no benchmarks found" in res.stderr


def test_run_stdin_rejects_variant(tmp_path):
    res = _mew("run", "--stdin", "--variant", "a=x.py", stdin="", cwd=tmp_path)
    assert res.returncode == 2
    assert "mutually exclusive" in res.stderr


def _native_profiler_available() -> bool:
    from mew import profilers

    try:
        profilers.select("auto")
        return True
    except SystemExit:
        return False


@pytest.mark.skipif(not _native_profiler_available(), reason="no native profiler backend")
def test_profile_stdin_filters_before_backend(benchdir, tmp_path):
    # A path-free stdin name filters discovery; a non-matching name selects
    # nothing, so profile exits ("no benchmarks found") before any backend runs —
    # which also proves --stdin is wired into profile's discovery.
    res = _mew("profile", str(benchdir), "--stdin", stdin="does_not_exist_xyz\n", cwd=tmp_path)
    assert res.returncode == 1
    assert "no benchmarks found" in res.stderr


def test_list_names_only_drops_path(benchdir, tmp_path):
    res = _mew("list", str(benchdir), "--names-only", cwd=tmp_path)
    assert res.returncode == 0, res.stderr
    names = [n for n in res.stdout.splitlines() if n.strip()]
    assert "bench_one" in names
    assert all("::" not in n for n in names)  # path-free identifiers


def test_list_names_only_show_cases(benchdir, tmp_path):
    res = _mew("list", str(benchdir), "--names-only", "--show-cases", cwd=tmp_path)
    assert res.returncode == 0, res.stderr
    names = [n for n in res.stdout.splitlines() if n.strip()]
    assert "bench_two[n=1]" in names and "bench_two[n=2]" in names
    assert all("::" not in n for n in names)


def test_run_stdin_names_only_is_cwd_independent(benchdir, tmp_path):
    # A path-free name (from --names-only) selects against run's own discovery
    # (here an absolute positional path), so the run cwd need not match the list
    # cwd — this is the fix for the relative-path round-trip.
    res = _mew(
        "run",
        str(benchdir),
        "--stdin",
        "--min-time",
        "1x",
        "--format",
        "jsonl",
        stdin="bench_one\n",
        cwd=tmp_path,  # cwd != benchdir
    )
    assert res.returncode == 0, res.stderr
    objs = [json.loads(x) for x in res.stdout.splitlines() if x.strip()]
    names = [o["name"] for o in objs if "name" in o]
    assert names and all("bench_one" in n for n in names)


def test_run_stdin_names_only_round_trip(benchdir, tmp_path):
    listing = _mew("list", str(benchdir), "--names-only", cwd=tmp_path)
    assert listing.returncode == 0, listing.stderr
    res = _mew(
        "run",
        str(benchdir),
        "--stdin",
        "--min-time",
        "1x",
        "--format",
        "jsonl",
        stdin=listing.stdout,
        cwd=tmp_path,
    )
    assert res.returncode == 0, res.stderr
    objs = [json.loads(x) for x in res.stdout.splitlines() if x.strip()]
    names = [o["name"] for o in objs if "name" in o]
    assert any("bench_one" in n for n in names)
    assert any("bench_two" in n for n in names)


def test_run_format_jsonl_streams_to_stdout(benchdir, tmp_path):
    # Every stdout line is valid JSON (no rich banner) → pipeable to `jq`.
    res = _mew("run", str(benchdir), "--min-time", "1x", "--format", "jsonl", cwd=tmp_path)
    assert res.returncode == 0, res.stderr
    objs = [json.loads(line) for line in res.stdout.splitlines() if line.strip()]
    names = [o["name"] for o in objs if "name" in o]
    assert any("bench_one" in n for n in names)
    assert any("bench_two" in n for n in names)


def test_run_format_json_to_stdout(benchdir, tmp_path):
    res = _mew("run", str(benchdir), "--min-time", "1x", "--format", "json", cwd=tmp_path)
    assert res.returncode == 0, res.stderr
    doc = json.loads(res.stdout)  # one well-formed document
    assert len(doc["benchmarks"]) == 3


def test_run_format_unknown_errors(benchdir, tmp_path):
    res = _mew("run", str(benchdir), "--min-time", "1x", "--format", "yaml", cwd=tmp_path)
    assert res.returncode == 2
    assert "unknown --format" in res.stderr


def test_run_format_without_stdout_sink_warns(benchdir, tmp_path):
    # --format only configures stdout; with file-only sinks it has nothing to do.
    out = tmp_path / "r.json"
    res = _mew(
        "run", str(benchdir), "--min-time", "1x", "--format", "jsonl", "-o", str(out), cwd=tmp_path
    )
    assert res.returncode == 0, res.stderr
    assert "no effect without a stdout sink" in res.stderr
    assert res.stdout.strip() == ""


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


# --- mew profile --slowest selection (in-process; no profiler backend needed) ---


def _ends(entries, suffix):
    return any(e.name.endswith(suffix) for e in entries)


def test_select_slowest_quick_pass():
    import mew
    from mew.cli import _select_slowest

    @mew.benchmark
    def bench_fast(state):
        for _ in state:
            pass

    @mew.benchmark
    def bench_mid(state):
        for _ in state:
            sum(range(1000))

    @mew.benchmark
    def bench_slow(state):
        for _ in state:
            sum(range(200_000))

    # No result file → a quick in-process timing pass ranks; the fast one drops.
    top2 = _select_slowest(mew.REGISTRY.all(), 2, rank_from=None)
    assert len(top2) == 2
    assert _ends(top2, "bench_slow") and _ends(top2, "bench_mid")
    assert not _ends(top2, "bench_fast")


def test_select_slowest_from_result_file(tmp_path):
    import mew
    from mew.cli import _select_slowest

    @mew.benchmark
    def bench_a(state):
        for _ in state:
            pass

    @mew.benchmark
    def bench_b(state):
        for _ in state:
            pass

    entries = mew.REGISTRY.all()
    name_a = next(e.name for e in entries if e.name.endswith("bench_a"))
    name_b = next(e.name for e in entries if e.name.endswith("bench_b"))
    # bench_b is the slower one in the recorded file.
    doc = {
        "context": {},
        "benchmarks": [
            {"name": name_a, "real_time": 10.0, "aggregate_name": ""},
            {"name": name_b, "real_time": 99.0, "aggregate_name": ""},
        ],
    }
    f = tmp_path / "r.json"
    f.write_text(json.dumps(doc))
    (top1,) = _select_slowest(entries, 1, rank_from=f)
    assert top1.name == name_b


def test_select_slowest_ranks_family_by_slowest_case(tmp_path):
    import mew
    from mew.cli import _select_slowest

    @mew.parametrize([{"n": 1}, {"n": 2}])
    def bench_fam(state, n):
        for _ in state:
            pass

    @mew.benchmark
    def bench_plain(state):
        for _ in state:
            pass

    entries = mew.REGISTRY.all()
    fam = next(e.name for e in entries if e.case_labels is not None)
    plain = next(e.name for e in entries if e.case_labels is None)
    # Family's slow case (with a GB option suffix) beats the plain benchmark;
    # the family's time is the max over its cases.
    doc = {
        "context": {},
        "benchmarks": [
            {"name": f"{fam}/case:0", "real_time": 1.0, "aggregate_name": ""},
            {"name": f"{fam}/case:1/min_time:0.200", "real_time": 99.0, "aggregate_name": ""},
            {"name": plain, "real_time": 50.0, "aggregate_name": ""},
        ],
    }
    f = tmp_path / "r.json"
    f.write_text(json.dumps(doc))
    (top1,) = _select_slowest(entries, 1, rank_from=f)
    assert top1.name == fam

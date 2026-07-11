"""CLI behavior, driven in-process via `mew.cli.main` for speed.

The `mew_cli` fixture invokes the real argparse pipeline and returns a
subprocess-shaped result (returncode/stdout/stderr), so tests read the same as
their end-to-end counterparts. A small section at the bottom keeps true
subprocess coverage: the module entry point, a real `mew list | mew run --stdin`
pipe, and the clean-error contract of the shipped binary (message + exit code,
no traceback), which only a fresh interpreter can prove.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path

import pytest
from _helpers import row as _row, write_pair as _write_pair

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


@dataclass
class _Result:
    """Mirrors the `subprocess.CompletedProcess` fields the tests read."""

    returncode: int
    stdout: str
    stderr: str


@pytest.fixture
def mew_cli(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path):
    """Invoke `mew.cli.main` in-process; returns a subprocess-shaped result.

    `cwd` chdirs for the call (undone at teardown), `stdin` replaces
    ``sys.stdin``, and a ``SystemExit`` becomes the returncode. The completion
    cache is redirected into tmp so discovery side effects stay out of the
    real ``~/.cache``.
    """
    from mew.cli import main

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / ".cache"))

    def invoke(*args: str, cwd: Path, stdin: str | None = None) -> _Result:
        monkeypatch.chdir(cwd)
        if stdin is not None:
            monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
        try:
            code = main(list(args))
        except SystemExit as e:
            if e.code is None or isinstance(e.code, int):
                code = e.code or 0
            else:
                # What the interpreter does with SystemExit("message"): print
                # it to stderr and exit 1.
                print(e.code, file=sys.stderr)
                code = 1
        captured = capsys.readouterr()
        return _Result(code or 0, captured.out, captured.err)

    return invoke


def test_list_discovers_all_entries(mew_cli, benchdir, tmp_path):
    res = mew_cli("list", str(benchdir), cwd=tmp_path)
    assert res.returncode == 0, res.stderr
    names = [n for n in res.stdout.splitlines() if n.strip()]
    # Parametrized families list as a single row; per-case expansion happens
    # at run time via Google Benchmark's family bookkeeping.
    assert any(n.endswith("::bench_one") for n in names)
    assert any(n.endswith("::bench_two") for n in names)


def test_list_pattern_filter(mew_cli, benchdir, tmp_path):
    res = mew_cli("list", str(benchdir), "-k", "bench_one", cwd=tmp_path)
    assert res.returncode == 0
    names = [n for n in res.stdout.splitlines() if n.strip()]
    assert all("bench_one" in n for n in names)
    assert names  # not empty


def test_list_no_matches_exits_nonzero(mew_cli, benchdir, tmp_path):
    res = mew_cli("list", str(benchdir), "-k", "nonexistent", cwd=tmp_path)
    assert res.returncode == 1


def test_list_works_from_subdirectory(mew_cli, benchdir, tmp_path):
    # Config benchpaths anchor at the project root (pyproject.toml), not cwd.
    (tmp_path / "pyproject.toml").write_text("[tool.mew]\n")
    sub = tmp_path / "src" / "pkg"
    sub.mkdir(parents=True)
    res = mew_cli("list", cwd=sub)
    assert res.returncode == 0, res.stderr
    assert "bench_one" in res.stdout


def test_list_pattern_is_regex(mew_cli, benchdir, tmp_path):
    # Alternation matches both fixture benchmarks; anchoring narrows to one.
    res = mew_cli("list", str(benchdir), "-k", "bench_(one|two)", cwd=tmp_path)
    assert res.returncode == 0, res.stderr
    names = [n for n in res.stdout.splitlines() if n.strip()]
    assert len(names) == 2

    res = mew_cli("list", str(benchdir), "-k", "bench_one$", cwd=tmp_path)
    names = [n for n in res.stdout.splitlines() if n.strip()]
    assert len(names) == 1 and names[0].endswith("::bench_one")


def test_run_invalid_pattern_errors(mew_cli, benchdir, tmp_path):
    res = mew_cli("run", str(benchdir), "--min-time", "1x", "-k", "foo(", cwd=tmp_path)
    assert res.returncode == 2
    assert "invalid benchmark filter pattern" in res.stderr


def test_run_k_selects_single_family_case(mew_cli, benchdir, tmp_path):
    # bench_two is parametrized [{n:1},{n:2}]; `n=2` addresses case index 1 only.
    out = tmp_path / "results.json"
    res = mew_cli(
        "run", str(benchdir), "--min-time", "1x", "-k", "n=2", "-o", str(out), cwd=tmp_path
    )
    assert res.returncode == 0, res.stderr
    benches = json.loads(out.read_text())["benchmarks"]
    assert len(benches) == 1
    assert "bench_two" in benches[0]["name"]
    assert "/case:1" in benches[0]["name"]


def test_list_show_cases_expands_family(mew_cli, benchdir, tmp_path):
    res = mew_cli("list", str(benchdir), "--show-cases", cwd=tmp_path)
    assert res.returncode == 0, res.stderr
    names = [n for n in res.stdout.splitlines() if n.strip()]
    # The family expands to one row per case; the plain benchmark stays single.
    assert any(n.endswith("::bench_two[n=1]") for n in names)
    assert any(n.endswith("::bench_two[n=2]") for n in names)
    assert any(n.endswith("::bench_one") for n in names)


def test_run_literal_selects_bracketed_case_without_escaping(mew_cli, benchdir, tmp_path):
    # `-F` lets a pasted `name[label]` select one case; the bare brackets would
    # otherwise be a regex char class and match nothing.
    out = tmp_path / "results.json"
    res = mew_cli(
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
    res = mew_cli("run", str(benchdir), "--min-time", "1x", "-k", "bench_two[n=2]", cwd=tmp_path)
    assert res.returncode == 1
    assert "no benchmarks found" in res.stderr


def test_list_k_shows_narrowed_family_cases(mew_cli, benchdir, tmp_path):
    res = mew_cli("list", str(benchdir), "-k", "n=2", cwd=tmp_path)
    assert res.returncode == 0, res.stderr
    names = [n for n in res.stdout.splitlines() if n.strip()]
    # The narrowed case is listed by label; the family and bench_one are gone.
    assert len(names) == 1
    assert names[0].endswith("::bench_two[n=2]")


def test_run_json_to_file(mew_cli, benchdir, tmp_path):
    out = tmp_path / "results.json"
    res = mew_cli("run", str(benchdir), "--min-time", "1x", "-o", str(out), cwd=tmp_path)
    assert res.returncode == 0, res.stderr
    doc = json.loads(out.read_text())
    assert len(doc["benchmarks"]) == 3


def test_run_nodeid_filter(mew_cli, benchdir, tmp_path):
    nodeid = f"{benchdir}/bench_fixture.py::bench_one"
    out = tmp_path / "results.json"
    res = mew_cli("run", nodeid, "--min-time", "1x", "-o", str(out), cwd=tmp_path)
    assert res.returncode == 0, res.stderr
    doc = json.loads(out.read_text())
    assert len(doc["benchmarks"]) == 1
    assert "bench_one" in doc["benchmarks"][0]["name"]


def test_run_rejects_unknown_output_format(mew_cli, benchdir, tmp_path):
    res = mew_cli("run", str(benchdir), "--min-time", "1x", "-o", "results.txt", cwd=tmp_path)
    assert res.returncode == 2
    assert "unsupported output format" in res.stderr


def test_list_filter_by_tag(mew_cli, benchdir, tmp_path):
    res = mew_cli("list", str(benchdir), "-t", "io", cwd=tmp_path)
    assert res.returncode == 0, res.stderr
    names = [n for n in res.stdout.splitlines() if n.strip()]
    assert all("bench_one" in n for n in names)
    assert names


def test_list_filter_by_multiple_tags_is_or(mew_cli, benchdir, tmp_path):
    res = mew_cli("list", str(benchdir), "-t", "io", "-t", "cpu", cwd=tmp_path)
    assert res.returncode == 0, res.stderr
    names = [n for n in res.stdout.splitlines() if n.strip()]
    # io picks bench_one, cpu picks the bench_two family → 2 entries
    # (the family expands to two Runs at run time, but `list` reports families).
    assert len(names) == 2


def test_list_show_tags(mew_cli, benchdir, tmp_path):
    res = mew_cli("list", str(benchdir), "--show-tags", cwd=tmp_path)
    assert res.returncode == 0, res.stderr
    assert "[io]" in res.stdout
    assert "[cpu]" in res.stdout


def test_run_jsonl_output_is_duckdb_queryable(mew_cli, benchdir, tmp_path):
    duckdb = pytest.importorskip("duckdb")

    out = tmp_path / "results.jsonl"
    res = mew_cli("run", str(benchdir), "--min-time", "1x", "-o", str(out), cwd=tmp_path)
    assert res.returncode == 0, res.stderr
    rows = duckdb.connect().execute(f"SELECT name, session_id FROM '{out}'").fetchall()
    assert len(rows) == 3
    assert all("bench_" in r[0] and r[1] for r in rows)


def test_run_jsonl_gz_extension_accepted(mew_cli, benchdir, tmp_path):
    out = tmp_path / "results.jsonl.gz"
    res = mew_cli("run", str(benchdir), "--min-time", "1x", "-o", str(out), cwd=tmp_path)
    assert res.returncode == 0, res.stderr
    import gzip

    with gzip.open(out, "rt") as fh:
        lines = fh.read().splitlines()
    assert len(lines) == 3  # pure NDJSON: one row per benchmark, no header


def test_run_min_warmup_time_accepts_durations(mew_cli, benchdir, tmp_path):
    # Consistent with --min-time's suffix syntax: `200ms` parses to seconds.
    res = mew_cli(
        "run", str(benchdir), "--min-time", "1x", "--min-warmup-time", "200ms", cwd=tmp_path
    )
    assert res.returncode == 0, res.stderr

    res = mew_cli("run", str(benchdir), "--min-warmup-time", "1h", cwd=tmp_path)
    assert res.returncode != 0
    assert "invalid --min-warmup-time" in res.stderr


def test_run_promoted_gb_flags_accepted(mew_cli, benchdir, tmp_path):
    # The promoted global knobs translate to GB flags GB actually accepts —
    # a bad flag would make benchmark::Initialize exit() before any run.
    out = tmp_path / "results.json"
    res = mew_cli(
        "run",
        str(benchdir),
        "--min-time",
        "1x",
        "--min-warmup-time",
        "0",
        "--repetitions",
        "2",
        "--random-interleaving",
        "-o",
        str(out),
        cwd=tmp_path,
    )
    assert res.returncode == 0, res.stderr
    doc = json.loads(out.read_text())
    # 3 benchmarks x 2 repetitions, plus GB aggregate rows for the repeats.
    per_rep = [b for b in doc["benchmarks"] if not b.get("aggregate_name")]
    assert len(per_rep) == 6
    assert all(b["iterations"] == 1 for b in per_rep)


def test_run_stdin_show_cases_selects_one_case_literally(mew_cli, benchdir):
    # A `name[label]` line from --show-cases is matched literally — no -F, no
    # bracket escaping — so exactly that one case runs.
    listing = mew_cli("list", ".", "--show-cases", "-k", "n=2", cwd=benchdir)
    assert listing.returncode == 0, listing.stderr
    assert listing.stdout.strip().endswith("bench_two[n=2]")
    res = mew_cli(
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


def test_run_stdin_empty_runs_nothing(mew_cli, benchdir):
    # Empty stdin selects nothing — it does not fall back to benchpaths.
    res = mew_cli("run", "--stdin", "--min-time", "1x", stdin="", cwd=benchdir)
    assert res.returncode == 1
    assert "no benchmarks found" in res.stderr


def test_run_stdin_rejects_variant(mew_cli, tmp_path):
    res = mew_cli("run", "--stdin", "--variant", "a=x.py", stdin="", cwd=tmp_path)
    assert res.returncode == 2
    assert "mutually exclusive" in res.stderr


def test_run_strict_rejects_variant(mew_cli, tmp_path):
    # --strict is not forwarded to variant children; erroring beats a silent
    # fall-back to the skip-and-warn default.
    res = mew_cli("run", "--strict", "--variant", "a=x.py", cwd=tmp_path)
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
def test_profile_stdin_filters_before_backend(mew_cli, benchdir, tmp_path):
    # A path-free stdin name filters discovery; a non-matching name selects
    # nothing, so profile exits ("no benchmarks found") before any backend runs —
    # which also proves --stdin is wired into profile's discovery.
    res = mew_cli("profile", str(benchdir), "--stdin", stdin="does_not_exist_xyz\n", cwd=tmp_path)
    assert res.returncode == 1
    assert "no benchmarks found" in res.stderr


class _FakeProfileBackend:
    """Stands in for a real native-frame backend so `profile --slowest` can be
    tested end-to-end without depending on xctrace/perf/py-spy being installed."""

    name = "fake"
    viewer_hint = "nowhere"

    def __init__(self) -> None:
        self.received_entries: list | None = None

    def run(self, entries, **kwargs):
        self.received_entries = list(entries)
        return {}


def test_profile_slowest_zero_is_usage_error(mew_cli, benchdir, tmp_path, monkeypatch):
    # cli.py's `--slowest` validation (`slowest < 1`) runs after backend
    # selection but before any entries are handed to the backend.
    import mew.profilers as profilers_mod

    backend = _FakeProfileBackend()
    monkeypatch.setattr(profilers_mod, "select", lambda name: backend)

    res = mew_cli("profile", str(benchdir), "--slowest", "0", cwd=tmp_path)
    assert res.returncode == 2
    assert "--slowest must be >= 1" in res.stderr
    assert backend.received_entries is None  # rejected before the backend ever ran


def test_profile_slowest_narrows_entries_reaching_backend(mew_cli, benchdir, tmp_path, monkeypatch):
    # End-to-end wiring: `--slowest 1` must narrow what the backend receives,
    # not just what `_select_slowest` returns in isolation.
    import mew.profilers as profilers_mod

    backend = _FakeProfileBackend()
    monkeypatch.setattr(profilers_mod, "select", lambda name: backend)

    res = mew_cli("profile", str(benchdir), "--slowest", "1", cwd=tmp_path)
    assert res.returncode == 0, res.stderr
    assert backend.received_entries is not None
    assert len(backend.received_entries) == 1
    assert "mew: profiling 1 slowest of 2" in res.stderr


def test_list_names_only_drops_path(mew_cli, benchdir, tmp_path):
    res = mew_cli("list", str(benchdir), "--names-only", cwd=tmp_path)
    assert res.returncode == 0, res.stderr
    names = [n for n in res.stdout.splitlines() if n.strip()]
    assert "bench_one" in names
    assert all("::" not in n for n in names)  # path-free identifiers


def test_list_names_only_show_cases(mew_cli, benchdir, tmp_path):
    res = mew_cli("list", str(benchdir), "--names-only", "--show-cases", cwd=tmp_path)
    assert res.returncode == 0, res.stderr
    names = [n for n in res.stdout.splitlines() if n.strip()]
    assert "bench_two[n=1]" in names and "bench_two[n=2]" in names
    assert all("::" not in n for n in names)


def test_run_stdin_names_only_is_cwd_independent(mew_cli, benchdir, tmp_path):
    # A path-free name (from --names-only) selects against run's own discovery
    # (here an absolute positional path), so the run cwd need not match the list
    # cwd — this is the fix for the relative-path round-trip.
    res = mew_cli(
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


def test_run_stdin_names_only_round_trip(mew_cli, benchdir, tmp_path):
    listing = mew_cli("list", str(benchdir), "--names-only", cwd=tmp_path)
    assert listing.returncode == 0, listing.stderr
    res = mew_cli(
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


def test_run_format_jsonl_streams_to_stdout(mew_cli, benchdir, tmp_path):
    # Every stdout line is valid JSON (no rich banner) → pipeable to `jq`.
    res = mew_cli("run", str(benchdir), "--min-time", "1x", "--format", "jsonl", cwd=tmp_path)
    assert res.returncode == 0, res.stderr
    objs = [json.loads(line) for line in res.stdout.splitlines() if line.strip()]
    names = [o["name"] for o in objs if "name" in o]
    assert any("bench_one" in n for n in names)
    assert any("bench_two" in n for n in names)


def test_run_format_json_to_stdout(mew_cli, benchdir, tmp_path):
    res = mew_cli("run", str(benchdir), "--min-time", "1x", "--format", "json", cwd=tmp_path)
    assert res.returncode == 0, res.stderr
    doc = json.loads(res.stdout)  # one well-formed document
    assert len(doc["benchmarks"]) == 3


def test_run_format_unknown_errors(mew_cli, benchdir, tmp_path):
    res = mew_cli("run", str(benchdir), "--min-time", "1x", "--format", "yaml", cwd=tmp_path)
    assert res.returncode == 2
    assert "unknown --format" in res.stderr


def test_run_format_without_stdout_sink_warns(mew_cli, benchdir, tmp_path):
    # --format only configures stdout; with file-only sinks it has nothing to do.
    out = tmp_path / "r.json"
    res = mew_cli(
        "run", str(benchdir), "--min-time", "1x", "--format", "jsonl", "-o", str(out), cwd=tmp_path
    )
    assert res.returncode == 0, res.stderr
    assert "no effect without a stdout sink" in res.stderr
    assert res.stdout.strip() == ""


def test_run_filter_by_tag(mew_cli, benchdir, tmp_path):
    out = tmp_path / "results.json"
    res = mew_cli(
        "run", str(benchdir), "--min-time", "1x", "-t", "cpu", "-o", str(out), cwd=tmp_path
    )
    assert res.returncode == 0, res.stderr
    doc = json.loads(out.read_text())
    names = [b["name"] for b in doc["benchmarks"]]
    assert len(names) == 2
    assert all("bench_two" in n for n in names)


# --- mew compare: --regression-threshold / --exit-non-zero-on-regression ---


def test_compare_regression_threshold_requires_percent_suffix(mew_cli, tmp_path):
    other, base = _write_pair(tmp_path, other=[], base=[])
    res = mew_cli("compare", str(other), str(base), "--regression-threshold", "5", cwd=tmp_path)
    assert res.returncode == 2  # usage error, not the "nothing matched" exit 1
    assert "--regression-threshold" in res.stderr
    assert "'5'" in res.stderr


def test_compare_regression_threshold_alone_is_report_only(mew_cli, tmp_path):
    # A regression is detected and printed, but without --exit-non-zero-on-regression
    # the command still exits 0 — the panel is informational, not a gate.
    # +20%, well over 5%:
    other, base = _write_pair(tmp_path, other=[_row("b", 120.0)], base=[_row("b", 100.0)])
    res = mew_cli("compare", str(other), str(base), "--regression-threshold", "5%", cwd=tmp_path)
    assert res.returncode == 0, res.stderr
    assert "❌" in res.stderr


def test_compare_exit_non_zero_on_regression_gates(mew_cli, tmp_path):
    # +20%, well over 5%:
    other, base = _write_pair(tmp_path, other=[_row("b", 120.0)], base=[_row("b", 100.0)])
    res = mew_cli(
        "compare",
        str(other),
        str(base),
        "--regression-threshold",
        "5%",
        "--exit-non-zero-on-regression",
        cwd=tmp_path,
    )
    assert res.returncode == 2
    assert "❌" in res.stderr


def test_compare_exit_non_zero_on_regression_gates_alone(mew_cli, tmp_path):
    # Without --regression-threshold / --regressions-config the gate flag
    # implies gating at the default threshold instead of silently no-opping.
    # +20%, over the 5% default:
    other, base = _write_pair(tmp_path, other=[_row("b", 120.0)], base=[_row("b", 100.0)])
    res = mew_cli("compare", str(other), str(base), "--exit-non-zero-on-regression", cwd=tmp_path)
    assert res.returncode == 2
    assert "❌" in res.stderr


def test_run_invalid_min_warmup_time_is_usage_error(mew_cli, tmp_path):
    # argparse type errors exit 2 (usage), not 1 (the "nothing matched" code).
    res = mew_cli("run", "--min-warmup-time", "nonsense", cwd=tmp_path)
    assert res.returncode == 2
    assert "--min-warmup-time" in res.stderr


# --- mew profile --slowest selection (in-process; no profiler backend needed) ---


def _ends(entries, suffix):
    return any(e.name.endswith(suffix) for e in entries)


def test_quick_timing_pass_collects_a_row_per_entry():
    # A real (but non-flaky, since it asserts no ordering) smoke test that the
    # in-process timing pass `_select_slowest` stubs out below actually runs
    # and produces a usable row per entry.
    import mew
    from mew.cli import _quick_timing_pass

    @mew.benchmark
    def bench_a(state):
        for _ in state:
            pass

    @mew.benchmark
    def bench_b(state):
        for _ in state:
            pass

    rows = _quick_timing_pass(mew.REGISTRY.all())
    assert len(rows) == 2
    assert all(isinstance(r.get("real_time"), float) for r in rows)


def test_select_slowest_quick_pass(monkeypatch):
    import mew
    from mew.cli import _select_slowest

    @mew.benchmark
    def bench_fast(state):
        for _ in state:
            pass

    @mew.benchmark
    def bench_mid(state):
        for _ in state:
            pass

    @mew.benchmark
    def bench_slow(state):
        for _ in state:
            pass

    entries = mew.REGISTRY.all()
    times = {"bench_fast": 1.0, "bench_mid": 50.0, "bench_slow": 500.0}
    # Stub the timing pass with deterministic numbers (mirrors
    # `test_select_slowest_ranks_family_by_slowest_case` below) instead of
    # relying on real wall-clock gaps between trivial benchmark bodies, which
    # is flake-prone under scheduler/CI noise.
    rows = [
        {
            "name": e.name,
            "real_time": next(t for k, t in times.items() if e.name.endswith(k)),
            "aggregate_name": "",
        }
        for e in entries
    ]
    monkeypatch.setattr("mew.cli._quick_timing_pass", lambda entries: rows)
    top2 = _select_slowest(entries, 2)
    assert len(top2) == 2
    assert _ends(top2, "bench_slow") and _ends(top2, "bench_mid")
    assert not _ends(top2, "bench_fast")


def test_select_slowest_ranks_family_by_slowest_case(monkeypatch):
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
    # the family's time is the max over its cases. Stub the timing pass so the
    # suffix-stripping logic is exercised with deterministic numbers.
    rows = [
        {"name": f"{fam}/case:0", "real_time": 1.0, "aggregate_name": ""},
        {"name": f"{fam}/case:1/min_time:0.200", "real_time": 99.0, "aggregate_name": ""},
        {"name": plain, "real_time": 50.0, "aggregate_name": ""},
    ]
    monkeypatch.setattr("mew.cli._quick_timing_pass", lambda entries: rows)
    (top1,) = _select_slowest(entries, 1)
    assert top1.name == fam


# --- shell completions ---


def test_completions_unknown_shell_errors(mew_cli, tmp_path):
    res = mew_cli("completions", "tcsh", cwd=tmp_path)
    assert res.returncode == 2
    assert "invalid choice" in res.stderr


@pytest.mark.parametrize(
    "shell,marker",
    [
        ("bash", "complete -F _mew mew"),
        ("zsh", "compdef _mew mew"),
        ("fish", "complete -c mew"),
    ],
)
def test_completions_generate(mew_cli, shell, marker, tmp_path):
    res = mew_cli("completions", shell, cwd=tmp_path)
    assert res.returncode == 0, res.stderr
    assert marker in res.stdout
    assert "run" in res.stdout and "compare" in res.stdout  # commands
    # a representative flag (fish renders long opts as `-l pattern`, so match the stem)
    assert "pattern" in res.stdout


@pytest.mark.skipif(not shutil.which("bash"), reason="bash not installed")
def test_completions_bash_functional(mew_cli, tmp_path):
    bash = shutil.which("bash")
    f = tmp_path / "c.bash"
    f.write_text(mew_cli("completions", "bash", cwd=tmp_path).stdout)
    assert subprocess.run([bash, "-n", str(f)]).returncode == 0  # syntax

    def complete(words: str, cword: int) -> list[str]:
        # Forward-slash, quoted path: a Windows path has backslashes, which bash
        # would treat as escapes inside `source ...`, silently failing to load.
        probe = (
            f"source '{f.as_posix()}'\n"
            f"COMP_WORDS=({words}); COMP_CWORD={cword}\n"
            "_mew\n"
            'printf "%s\\n" "${COMPREPLY[@]}"\n'
        )
        return subprocess.run([bash, "-c", probe], capture_output=True, text=True).stdout.split()

    assert "run" in complete("mew ru", 1)  # subcommand
    assert "--pattern" in complete("mew run --pa", 2)  # option flag
    assert set(complete("mew run --format ''", 3)) == {"json", "jsonl", "rich"}  # choices
    assert set(complete("mew completions ''", 2)) == {"bash", "zsh", "fish"}


def test_completions_profile_format_has_backend_values(mew_cli, tmp_path):
    # run and profile share the `format` dest but have disjoint value sets;
    # profile must not offer run's rich/json/jsonl.
    bash = mew_cli("completions", "bash", cwd=tmp_path).stdout
    profile_block = bash[bash.index("    profile)") :]
    line = next(ln for ln in profile_block.splitlines() if "--format" in ln and "compgen" in ln)
    assert "speedscope" in line and "xctrace" in line
    assert "jsonl" not in line


def test_completions_bash_positional_falls_back_to_files(mew_cli, tmp_path):
    # run/list/profile take `path[::filter]` selectors; bash completes the path part.
    bash = mew_cli("completions", "bash", cwd=tmp_path).stdout
    run_block = bash[bash.index("    run)") : bash.index("    profile)")]
    assert 'COMPREPLY=( $(compgen -f -- "$cur") )\n        ;;' in run_block


def test_completions_zsh_repeatable_options_star_form(mew_cli, tmp_path):
    zsh = mew_cli("completions", "zsh", cwd=tmp_path).stdout
    # Repeatable -t/--tag: `*` prefix and braces outside the quotes; no
    # self-exclusion that would stop zsh offering it a second time.
    assert "'*'{-t,--tag}'" in zsh
    assert "(-t --tag)" not in zsh
    # Non-repeatable multi-flag options keep the exclusion group.
    assert "'(-k --pattern)'{-k,--pattern}'" in zsh


@pytest.mark.skipif(not shutil.which("zsh"), reason="zsh not installed")
def test_completions_zsh_syntax(mew_cli, tmp_path):
    f = tmp_path / "c.zsh"
    f.write_text(mew_cli("completions", "zsh", cwd=tmp_path).stdout)
    assert subprocess.run(["zsh", "-n", str(f)]).returncode == 0


@pytest.mark.skipif(not shutil.which("fish"), reason="fish not installed")
def test_completions_fish_syntax(mew_cli, tmp_path):
    f = tmp_path / "c.fish"
    f.write_text(mew_cli("completions", "fish", cwd=tmp_path).stdout)
    assert subprocess.run(["fish", "--no-execute", str(f)]).returncode == 0


# --- end-to-end: the real entry point in a subprocess -------------------------
#
# Everything above runs in-process for speed; this section proves the shipped
# binary: `python -m mew.cli` imports and runs in a fresh interpreter, real
# pipes round-trip, and errors reach the user as a message + exit code with no
# traceback.


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


def test_e2e_run_both_sinks(benchdir, tmp_path):
    out = tmp_path / "results.json"
    res = _mew(
        "run", str(benchdir), "--min-time", "1x", "-o", "stdout", "-o", str(out), cwd=tmp_path
    )
    assert res.returncode == 0, res.stderr
    # Rich table on stdout AND a JSON file on disk.
    assert "Benchmark" in res.stdout
    doc = json.loads(out.read_text())
    assert len(doc["benchmarks"]) == 3


def test_e2e_run_stdin_round_trip(benchdir):
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


def test_e2e_missing_default_benchpath_is_clean(tmp_path):
    # A fresh project without benchmarks/ is "nothing found", not a traceback.
    res = _mew("list", cwd=tmp_path)
    assert res.returncode == 1
    assert "no benchmarks found" in res.stderr
    assert "Traceback" not in res.stderr


def test_e2e_explicit_missing_path_is_clean_error(tmp_path):
    res = _mew("list", "nonexistent_dir", cwd=tmp_path)
    assert res.returncode == 2
    assert "path does not exist" in res.stderr
    assert "Traceback" not in res.stderr


def test_e2e_malformed_config_is_clean_error(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[tool.mew]\nbenchpaths = 42\n")
    res = _mew("list", cwd=tmp_path)
    assert res.returncode == 2
    assert "invalid [tool.mew] config" in res.stderr
    assert "Traceback" not in res.stderr

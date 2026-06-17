"""CLI sanity: `mew list` and `mew run` via subprocess against a fixture file."""

from __future__ import annotations

import json
import os
import shutil
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


def test_list_missing_default_benchpath_is_clean(tmp_path):
    # A fresh project without benchmarks/ is "nothing found", not a traceback.
    res = _mew("list", cwd=tmp_path)
    assert res.returncode == 1
    assert "no benchmarks found" in res.stderr
    assert "Traceback" not in res.stderr


def test_explicit_missing_path_is_clean_error(tmp_path):
    res = _mew("list", "nonexistent_dir", cwd=tmp_path)
    assert res.returncode == 2
    assert "path does not exist" in res.stderr
    assert "Traceback" not in res.stderr


def test_list_works_from_subdirectory(benchdir, tmp_path):
    # Config benchpaths anchor at the project root (pyproject.toml), not cwd.
    (tmp_path / "pyproject.toml").write_text("[tool.mew]\n")
    sub = tmp_path / "src" / "pkg"
    sub.mkdir(parents=True)
    res = _mew("list", cwd=sub)
    assert res.returncode == 0, res.stderr
    assert "bench_one" in res.stdout


def test_malformed_config_is_clean_error(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[tool.mew]\nbenchpaths = 42\n")
    res = _mew("list", cwd=tmp_path)
    assert res.returncode == 2
    assert "invalid [tool.mew] config" in res.stderr
    assert "Traceback" not in res.stderr


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


def test_run_jsonl_output_is_duckdb_queryable(benchdir, tmp_path):
    duckdb = pytest.importorskip("duckdb")

    out = tmp_path / "results.jsonl"
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
    rows = duckdb.connect().execute(f"SELECT name, session_id FROM '{out}'").fetchall()
    assert len(rows) == 3
    assert all("bench_" in r[0] and r[1] for r in rows)


def test_run_jsonl_gz_extension_accepted(benchdir, tmp_path):
    out = tmp_path / "results.jsonl.gz"
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
    import gzip

    with gzip.open(out, "rt") as fh:
        lines = fh.read().splitlines()
    assert len(lines) == 3  # pure NDJSON: one row per benchmark, no header


def test_run_min_warmup_time_accepts_durations(benchdir, tmp_path):
    # Consistent with --min-time's suffix syntax: `200ms` parses to seconds.
    res = _mew("run", str(benchdir), "--min-time", "1x", "--min-warmup-time", "200ms", cwd=tmp_path)
    assert res.returncode == 0, res.stderr

    res = _mew("run", str(benchdir), "--min-warmup-time", "1h", cwd=tmp_path)
    assert res.returncode != 0
    assert "invalid --min-warmup-time" in res.stderr
    assert "Traceback" not in res.stderr


def test_run_promoted_gb_flags_accepted(benchdir, tmp_path):
    # The promoted global knobs translate to GB flags GB actually accepts —
    # a bad flag would make benchmark::Initialize exit() before any run.
    out = tmp_path / "results.json"
    res = _mew(
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

    # A quick in-process timing pass ranks; the fast one drops.
    top2 = _select_slowest(mew.REGISTRY.all(), 2)
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


def test_completions_unknown_shell_errors(tmp_path):
    res = _mew("completions", "tcsh", cwd=tmp_path)
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
def test_completions_generate(shell, marker, tmp_path):
    res = _mew("completions", shell, cwd=tmp_path)
    assert res.returncode == 0, res.stderr
    assert marker in res.stdout
    assert "run" in res.stdout and "compare" in res.stdout  # commands
    # a representative flag (fish renders long opts as `-l pattern`, so match the stem)
    assert "pattern" in res.stdout


def test_completions_bash_functional(tmp_path):
    bash = shutil.which("bash")
    assert bash, "bash is expected on supported platforms"
    f = tmp_path / "c.bash"
    f.write_text(_mew("completions", "bash", cwd=tmp_path).stdout)
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


def test_completions_profile_format_has_backend_values(tmp_path):
    # run and profile share the `format` dest but have disjoint value sets;
    # profile must not offer run's rich/json/jsonl.
    bash = _mew("completions", "bash", cwd=tmp_path).stdout
    profile_block = bash[bash.index("    profile)") :]
    line = next(ln for ln in profile_block.splitlines() if "--format" in ln and "compgen" in ln)
    assert "speedscope" in line and "xctrace" in line
    assert "jsonl" not in line


def test_completions_bash_positional_falls_back_to_files(tmp_path):
    # run/list/profile take `path[::filter]` selectors; bash completes the path part.
    bash = _mew("completions", "bash", cwd=tmp_path).stdout
    run_block = bash[bash.index("    run)") : bash.index("    profile)")]
    assert 'COMPREPLY=( $(compgen -f -- "$cur") )\n        ;;' in run_block


def test_completions_zsh_repeatable_options_star_form(tmp_path):
    zsh = _mew("completions", "zsh", cwd=tmp_path).stdout
    # Repeatable -t/--tag: `*` prefix and braces outside the quotes; no
    # self-exclusion that would stop zsh offering it a second time.
    assert "'*'{-t,--tag}'" in zsh
    assert "(-t --tag)" not in zsh
    # Non-repeatable multi-flag options keep the exclusion group.
    assert "'(-k --pattern)'{-k,--pattern}'" in zsh


@pytest.mark.skipif(not shutil.which("zsh"), reason="zsh not installed")
def test_completions_zsh_syntax(tmp_path):
    f = tmp_path / "c.zsh"
    f.write_text(_mew("completions", "zsh", cwd=tmp_path).stdout)
    assert subprocess.run(["zsh", "-n", str(f)]).returncode == 0


@pytest.mark.skipif(not shutil.which("fish"), reason="fish not installed")
def test_completions_fish_syntax(tmp_path):
    f = tmp_path / "c.fish"
    f.write_text(_mew("completions", "fish", cwd=tmp_path).stdout)
    assert subprocess.run(["fish", "--no-execute", str(f)]).returncode == 0

"""CLI behavior, driven in-process via `mew.cli.main` for speed.

The end-to-end section at the bottom uses real subprocesses: only a fresh
interpreter proves the entry point, real pipes, and the no-traceback contract.
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
from _helpers import row as _row, write_json as _write_json, write_pair as _write_pair

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
def mew_cli(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    """Invoke `mew.cli.main` in-process; returns a subprocess-shaped result."""
    from mew.cli import main

    # Output assertions expect plain text unless a test opts into color.
    monkeypatch.delenv("FORCE_COLOR", raising=False)

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
    # A parametrized family lists as one row without --show-cases.
    assert any(n.endswith("::bench_one") for n in names)
    assert any(n.endswith("::bench_two") for n in names)


@pytest.mark.parametrize(
    "args",
    [("-h",), ("list", "-h"), ("run", "-h"), ("compare", "-h"), ("completions", "-h")],
)
def test_help_is_plain_terminal_text(mew_cli, tmp_path, args):
    res = mew_cli(*args, cwd=tmp_path)
    assert res.returncode == 0
    assert "usage: mew" in res.stdout
    assert "\x1b[" not in res.stdout


def test_help_honors_force_color(mew_cli, tmp_path, monkeypatch):
    # Like the reporters and tables: FORCE_COLOR colors even redirected output.
    monkeypatch.setenv("FORCE_COLOR", "1")
    res = mew_cli("-h", cwd=tmp_path)
    assert res.returncode == 0
    assert "\x1b[" in res.stdout


def test_list_selects_symlinked_bench_file_in_benchpath(mew_cli, tmp_path):
    # The link is imported through the benchpath, so it must be selected there
    # even though its target lives outside.
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "impl.py").write_text(textwrap.dedent(FIXTURE))
    bench = tmp_path / "benchmarks"
    bench.mkdir()
    try:
        (bench / "bench_link.py").symlink_to(shared / "impl.py")
    except OSError:
        pytest.skip("symlinks are not available")
    res = mew_cli("list", cwd=tmp_path)
    assert res.returncode == 0, res.stderr
    assert "bench_one" in res.stdout


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

    res = mew_cli("run", str(benchdir), "--min-time", "1x", "-k", "bench_two[n=2]", cwd=tmp_path)
    assert res.returncode == 1
    assert "no benchmarks found" in res.stderr


def test_list_k_shows_narrowed_family_cases(mew_cli, benchdir, tmp_path):
    res = mew_cli("list", str(benchdir), "-k", "n=2", cwd=tmp_path)
    assert res.returncode == 0, res.stderr
    names = [n for n in res.stdout.splitlines() if n.strip()]
    assert len(names) == 1
    assert names[0].endswith("::bench_two[n=2]")


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
    # io picks bench_one, cpu picks the bench_two family, listed as one row.
    assert {n.rsplit("::", 1)[-1] for n in names} == {"bench_one", "bench_two"}
    assert len(names) == 2


def test_list_show_tags(mew_cli, benchdir, tmp_path):
    res = mew_cli("list", str(benchdir), "--show-tags", cwd=tmp_path)
    assert res.returncode == 0, res.stderr
    assert "[io]" in res.stdout
    assert "[cpu]" in res.stdout


def test_run_jsonl_gz_extension_accepted(mew_cli, benchdir, tmp_path):
    out = tmp_path / "results.jsonl.gz"
    res = mew_cli("run", str(benchdir), "--min-time", "1x", "-o", str(out), cwd=tmp_path)
    assert res.returncode == 0, res.stderr
    import gzip

    with gzip.open(out, "rt") as fh:
        lines = fh.read().splitlines()
    assert len(lines) == 3  # pure NDJSON: one row per benchmark, no header


@pytest.mark.parametrize(
    ("value", "seconds"), [("0.5", 0.5), ("200ms", 0.2), ("10s", 10.0), ("1m", 60.0)]
)
def test_min_warmup_time_parses_durations(value, seconds):
    # Consistent with --min-time's suffix syntax: `200ms` parses to seconds.
    from mew.cli import _warmup_seconds

    assert _warmup_seconds(value) == seconds


@pytest.mark.parametrize(
    ("flag", "value"),
    [
        ("--min-time", "0x"),
        ("--min-time", "nan"),
        ("--min-warmup-time", "-1"),
        ("--min-warmup-time", "nonsense"),
        ("--repetitions", "0"),
        ("--memory-iterations", "0"),
        ("--sample-interval", "0"),
        ("--sample-top-k", "0"),
        ("--sampler", "perf"),
    ],
)
def test_run_rejects_invalid_option_values(mew_cli, benchdir, tmp_path, flag, value):
    # argparse type errors exit 2 (usage), not 1 (the "nothing matched" code).
    res = mew_cli("run", str(benchdir), flag, value, cwd=tmp_path)
    assert res.returncode == 2
    assert flag in res.stderr


@pytest.mark.parametrize(
    ("outputs", "message"),
    [
        ((), "--append requires"),
        (("-o", "out.json"), "--append requires"),
        # With a JSONL sink present, the JSON sink is still rejected.
        (("-o", "out.jsonl", "-o", "out.json"), "--append is not supported for the JSON sink"),
    ],
)
def test_run_append_requires_jsonl_file(mew_cli, benchdir, tmp_path, outputs, message):
    # Neither stdout nor a single-document JSON file can be appended to.
    res = mew_cli("run", str(benchdir), "--append", *outputs, cwd=tmp_path)
    assert res.returncode == 2
    assert message in res.stderr


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


def test_list_names_only_drops_path(mew_cli, benchdir, tmp_path):
    res = mew_cli("list", str(benchdir), "--names-only", cwd=tmp_path)
    assert res.returncode == 0, res.stderr
    names = [n for n in res.stdout.splitlines() if n.strip()]
    assert "bench_one" in names
    assert all("::" not in n for n in names)


def test_list_names_only_show_cases(mew_cli, benchdir, tmp_path):
    res = mew_cli("list", str(benchdir), "--names-only", "--show-cases", cwd=tmp_path)
    assert res.returncode == 0, res.stderr
    names = [n for n in res.stdout.splitlines() if n.strip()]
    assert "bench_two[n=1]" in names and "bench_two[n=2]" in names
    assert all("::" not in n for n in names)


def test_run_stdin_names_only_round_trip_is_cwd_independent(mew_cli, benchdir, tmp_path):
    # A path-free name selects against run's own discovery, so the run cwd
    # need not match the bench dir.
    listing = mew_cli("list", str(benchdir), "--names-only", "-k", "bench_one", cwd=tmp_path)
    assert listing.returncode == 0, listing.stderr
    assert listing.stdout.splitlines() == ["bench_one"]
    res = mew_cli(
        "run",
        str(benchdir),
        "--stdin",
        "--min-time",
        "1x",
        "--format",
        "jsonl",
        stdin=listing.stdout,
        cwd=tmp_path,  # cwd != benchdir
    )
    assert res.returncode == 0, res.stderr
    objs = [json.loads(x) for x in res.stdout.splitlines() if x.strip()]
    names = [o["name"] for o in objs if "name" in o]
    assert len(names) == 1 and names[0].endswith("::bench_one")


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
    doc = json.loads(res.stdout)
    assert len(doc["benchmarks"]) == 3


def test_run_format_unknown_errors(mew_cli, benchdir, tmp_path):
    res = mew_cli("run", str(benchdir), "--min-time", "1x", "--format", "yaml", cwd=tmp_path)
    assert res.returncode == 2
    assert "invalid choice: 'yaml'" in res.stderr


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


@pytest.mark.parametrize(
    ("config", "flag", "captured"),
    [("", None, True), ('capture = "no"', None, False), ('capture = "no"', "fd", True)],
)
def test_capture_setting_and_flag_precedence(
    monkeypatch, capfd: pytest.CaptureFixture[str], tmp_path, config, flag, captured
):
    # `main()` directly, not `mew_cli`: uncaptured, the body writes to the real
    # fd 2, which only capfd sees (and capfd cannot share a test with capsys).
    from mew.cli import main

    (tmp_path / "pyproject.toml").write_text(f"[tool.mew]\n{config}\n")
    (tmp_path / "bench_noisy.py").write_text(
        textwrap.dedent(
            """
            import os

            import mew

            @mew.benchmark(iterations=1)
            def noisy(state):
                os.write(2, b"from the benchmark\\n")
                for _ in state:
                    pass
            """
        )
    )
    monkeypatch.chdir(tmp_path)
    assert main(["run", "bench_noisy.py", *(["--capture", flag] if flag else [])]) == 0
    err = capfd.readouterr().err
    assert ("output from benchmarks:" in err) == captured
    assert "from the benchmark" in err


_PROFILED = """
    import mew

    def spin():
        # Python-level work, so the sampled self time lands in this frame.
        total = 0
        for i in range(20_000):
            total += i
        return total

    @mew.benchmark
    def bench_hot(state):
        for _ in state:
            spin()
            buf = bytearray(1 << 20)
            del buf
"""


@pytest.mark.parametrize(
    ("module", "flags"),
    [
        ("memray", ("--profile-memory", "--flamegraph")),
        pytest.param(
            "profiling.sampling" if sys.version_info >= (3, 15) else "pyinstrument",
            ("--sample", "--sample-interval", "1e-5", "--sample-top-k", "2", "--sample-html"),
            marks=pytest.mark.skipif(
                sys.version_info < (3, 15) and not getattr(sys, "_is_gil_enabled", lambda: True)(),
                reason="pyinstrument's native sampler enables the GIL",
            ),
        ),
    ],
)
def test_run_profiling_flags_write_the_report_and_annotate_rows(mew_cli, tmp_path, module, flags):
    pytest.importorskip(module)
    from mew.reporter import _fmt_bytes

    (tmp_path / "bench_profiled.py").write_text(textwrap.dedent(_PROFILED))
    report, out = tmp_path / "report.html", tmp_path / "rows.json"
    res = mew_cli(
        "run",
        "bench_profiled.py",
        "--min-time",
        "20x",
        *flags,
        str(report),
        "-o",
        "stdout",
        "-o",
        str(out),
        cwd=tmp_path,
    )
    assert res.returncode == 0, res.stderr
    # The report renders from the run's own profiler pass, without a rerun.
    assert "bench_hot" in report.read_text()
    (row,) = json.loads(out.read_text())["benchmarks"]
    # The row carries the profile, and the console table shows it.
    if module == "memray":
        assert row["memory"]["peak_bytes"] >= 1 << 20  # the loop's 1 MiB buffer
        assert _fmt_bytes(row["memory"]["peak_bytes"]) in res.stdout
    else:
        from mew.cpu import reads_instructions

        top = row["cpu_profile"]["top_function"]
        assert "spin" in top["function"]
        assert f"{top['function']} ({top['file']}" in res.stdout
        # Only free-threaded Tachyon reads bytecode instructions.
        assert ("Hottest Instruction" in res.stdout) is reads_instructions()
        assert bool(row["cpu_profile"]["top_instructions"]) is reads_instructions()
        assert len(row["cpu_profile"]["top_instructions"]) <= 2


@pytest.mark.skipif(sys.version_info >= (3, 15), reason="Tachyon runs here")
def test_run_rejects_tachyon_before_python_3_15(mew_cli, benchdir, tmp_path):
    res = mew_cli("run", str(benchdir), "--sample", "--sampler", "tachyon", cwd=tmp_path)
    assert res.returncode == 1
    assert "requires Python 3.15" in res.stderr


@pytest.mark.skipif(sys.version_info < (3, 15), reason="pyinstrument is the default here")
@pytest.mark.skipif(
    not getattr(sys, "_is_gil_enabled", lambda: True)(),
    reason="pyinstrument's native sampler enables the GIL",
)
def test_run_samples_with_pyinstrument_on_python_3_15(mew_cli, tmp_path):
    """One sampler across a suite's interpreter range: pyinstrument on 3.15 too."""
    pytest.importorskip("pyinstrument")
    (tmp_path / "bench_profiled.py").write_text(textwrap.dedent(_PROFILED))
    out = tmp_path / "rows.json"
    res = mew_cli(
        "run",
        "bench_profiled.py",
        "--min-time",
        "20x",
        "--sample",
        "--sampler",
        "pyinstrument",
        "--sample-top-k",
        "2",
        "-o",
        "stdout",
        "-o",
        str(out),
        cwd=tmp_path,
    )
    assert res.returncode == 0, res.stderr
    (row,) = json.loads(out.read_text())["benchmarks"]
    assert row["cpu_profile"]["profiler"] == "pyinstrument"
    assert "spin" in row["cpu_profile"]["top_function"]["function"]
    # pyinstrument reads no instructions, whatever --sample-top-k says.
    assert row["cpu_profile"]["top_instructions"] == []
    assert "Hottest Instruction" not in res.stdout


# --- mew compare: --regression-threshold / --exit-non-zero-on-regression ---


def test_compare_regression_threshold_requires_percent_suffix(mew_cli, tmp_path):
    other, base = _write_pair(tmp_path, other=[], base=[])
    res = mew_cli("compare", str(other), str(base), "--regression-threshold", "5", cwd=tmp_path)
    assert res.returncode == 2  # usage error, not the "nothing matched" exit 1
    assert "--regression-threshold" in res.stderr
    assert "'5'" in res.stderr


@pytest.mark.parametrize("value", ["-1%", "nan%", "inf%"])
def test_compare_regression_threshold_must_be_non_negative_and_finite(mew_cli, tmp_path, value):
    other, base = _write_pair(tmp_path, other=[], base=[])
    res = mew_cli("compare", str(other), str(base), "--regression-threshold", value, cwd=tmp_path)
    assert res.returncode == 2


@pytest.mark.parametrize(
    ("flags", "code"),
    [
        # Without --exit-non-zero-on-regression the panel is informational, not a gate.
        (("--regression-threshold", "5%"), 0),
        (("--regression-threshold", "5%", "--exit-non-zero-on-regression"), 2),
        # The gate flag alone gates at the default threshold rather than no-opping.
        (("--exit-non-zero-on-regression",), 2),
    ],
)
def test_compare_exit_non_zero_on_regression_gates(mew_cli, tmp_path, flags, code):
    # +20%, well over both the 5% threshold and the 5% default:
    other, base = _write_pair(tmp_path, other=[_row("b", 120.0)], base=[_row("b", 100.0)])
    res = mew_cli("compare", str(other), str(base), *flags, cwd=tmp_path)
    assert res.returncode == code, res.stderr
    assert "❌" in res.stderr


def test_compare_malformed_regressions_config_is_usage_error(mew_cli, tmp_path):
    # A broken allow rule is a CLI error, not a ValueError traceback.
    other, base = _write_pair(tmp_path, other=[_row("b", 1.0)], base=[_row("b", 1.0)])
    (tmp_path / "pyproject.toml").write_text(
        '[[tool.mew.regressions.allow]]\npattern = "b"\nignore = true\n'
    )
    res = mew_cli("compare", str(other), str(base), "--regression-threshold", "5%", cwd=tmp_path)
    assert res.returncode == 2
    assert "invalid regressions config" in res.stderr
    assert "reason" in res.stderr
    assert "Traceback" not in res.stderr


def test_sessions_lists_newest_first(mew_cli, tmp_path):
    p = tmp_path / "r.json"
    _write_json(
        p,
        [
            _row(
                "b", 1.0, date="2026-01-01T00:00:00", session_id="aaaaaaaa-1", session_tag="before"
            ),
            _row(
                "b", 2.0, date="2026-02-01T00:00:00", session_id="bbbbbbbb-2", session_tag="after"
            ),
        ],
    )
    res = mew_cli("sessions", str(p), cwd=tmp_path)
    assert res.returncode == 0, res.stderr
    lines = res.stdout.splitlines()
    assert lines[0].startswith("Id")
    assert lines[2].startswith("bbbbbbbb") and "after" in lines[2]
    assert lines[3].startswith("aaaaaaaa") and "before" in lines[3]


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
    assert "run" in res.stdout and "compare" in res.stdout
    # fish renders long opts as `-l pattern`, so match the stem.
    assert "pattern" in res.stdout
    # Help text appears as argparse renders it, with "%%" unescaped.
    assert "%%" not in res.stdout


@pytest.fixture
def bash_complete(mew_cli, tmp_path):
    """Source the generated bash completion; returns a `COMPREPLY` probe."""
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash not installed")
    f = tmp_path / "c.bash"
    f.write_text(mew_cli("completions", "bash", cwd=tmp_path).stdout)
    assert subprocess.run([bash, "-n", str(f)]).returncode == 0

    def complete(words: str, cword: int, cwd: Path | None = None) -> list[str]:
        # Forward-slash, quoted path: a Windows path has backslashes, which bash
        # would treat as escapes inside `source ...`, silently failing to load.
        probe = (
            f"source '{f.as_posix()}'\n"
            f"COMP_WORDS=({words}); COMP_CWORD={cword}\n"
            "_mew\n"
            'printf "%s\\n" "${COMPREPLY[@]}"\n'
        )
        res = subprocess.run([bash, "-c", probe], capture_output=True, text=True, cwd=cwd)
        return res.stdout.split()

    return complete


def test_completions_bash_functional(bash_complete):
    assert "run" in bash_complete("mew ru", 1)
    assert "--pattern" in bash_complete("mew run --pa", 2)
    assert set(bash_complete("mew run --format ''", 3)) == {"json", "jsonl", "rich"}
    assert set(bash_complete("mew completions ''", 2)) == {"bash", "zsh", "fish"}


def test_completions_bash_positional_falls_back_to_files(bash_complete, tmp_path):
    # run/list take `path[::filter]` selectors; bash completes the path part.
    project = tmp_path / "project"
    project.mkdir()
    (project / "bench_known.py").touch()
    assert bash_complete("mew run ''", 2, cwd=project) == ["bench_known.py"]


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
# `python -m mew.cli` in a fresh interpreter: real pipes round-trip, and errors
# are a message and an exit code, not a traceback.


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


def test_e2e_run_stdin_round_trip(benchdir, tmp_path):
    # `mew list | mew run --stdin` with no xargs; run from the discovery cwd so
    # the relative paths in the listing resolve.
    listing = _mew("list", ".", cwd=benchdir)
    assert listing.returncode == 0, listing.stderr
    out = tmp_path / "results.json"
    res = _mew(
        "run",
        "--stdin",
        "--min-time",
        "1x",
        "--format",
        "jsonl",
        "-o",
        "stdout",
        "-o",
        str(out),
        stdin=listing.stdout,
        cwd=benchdir,
    )
    assert res.returncode == 0, res.stderr
    objs = [json.loads(x) for x in res.stdout.splitlines() if x.strip()]
    names = [o["name"] for o in objs if "name" in o]
    assert any("bench_one" in n for n in names)
    assert any("bench_two" in n for n in names)
    # Both sinks receive every row.
    assert [b["name"] for b in json.loads(out.read_text())["benchmarks"]] == names
    assert len(names) == 3


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


def _setup_project(tmp_path: Path, setup_body: str) -> Path:
    """A project whose `[tool.mew] setup` file runs before discovery."""
    (tmp_path / "benchmarks").mkdir()
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "demo"\nversion = "0.1.0"\n\n[tool.mew]\nsetup = "benchmarks/conf.py"\n'
    )
    (tmp_path / "benchmarks" / "conf.py").write_text(setup_body)
    for name in ("a", "b"):
        (tmp_path / "benchmarks" / f"bench_{name}.py").write_text(
            f'import mew\n\n\n@mew.benchmark(name="{name}")\n'
            f"def bench_{name}(state):\n    for _ in state:\n        pass\n"
        )
    return tmp_path


def test_setup_file_context_applies_to_a_single_file_run(mew_cli, tmp_path: Path):
    """Setup-file context must not depend on which benchmark files are selected."""
    _setup_project(tmp_path, 'import mew\n\nmew.set_context("team", "perf")\n')
    out = tmp_path / "one.json"
    res = mew_cli("run", "benchmarks/bench_b.py", "--min-time=1x", "-o", str(out), cwd=tmp_path)
    assert res.returncode == 0, res.stderr
    doc = json.loads(out.read_text())
    assert doc["context"]["team"] == "perf"
    assert [b["name"].rsplit("::", 1)[-1] for b in doc["benchmarks"]] == ["b"]


def test_setup_file_missing_is_a_clear_error(mew_cli, tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "demo"\nversion = "0.1.0"\n\n[tool.mew]\nsetup = "nope.py"\n'
    )
    (tmp_path / "benchmarks").mkdir()
    res = mew_cli("run", "--min-time=1x", cwd=tmp_path)
    # A config error (2), not the "no benchmarks found" code (1).
    assert res.returncode == 2
    assert "setup file not found" in res.stderr


@pytest.mark.parametrize("spec", ["bogus", "p101"])
def test_compare_invalid_statistic_is_a_usage_error(mew_cli, tmp_path: Path, spec):
    # 2, not the "no overlap" code (1); checked before any file is read.
    res = mew_cli("compare", "--statistic", spec, "a.json", "b.json", cwd=tmp_path)
    assert res.returncode == 2
    assert "statistic" in res.stderr


@pytest.mark.parametrize("second_filter", ["::bench_two", ""])
def test_path_filters_are_scoped_to_their_files(mew_cli, tmp_path, second_filter):
    for name in ("a", "b"):
        (tmp_path / f"bench_{name}.py").write_text(textwrap.dedent(FIXTURE))
    res = mew_cli("list", "bench_a.py::bench_one", f"bench_b.py{second_filter}", cwd=tmp_path)
    assert res.returncode == 0, res.stderr
    expected = {"bench_a.py::bench_one", "bench_b.py::bench_two"}
    if not second_filter:
        expected.add("bench_b.py::bench_one")
    assert set(res.stdout.splitlines()) == expected


def test_stdin_path_selectors_do_not_cross_select_family_cases(mew_cli, tmp_path):
    for name in ("a", "b"):
        (tmp_path / f"bench_{name}.py").write_text(textwrap.dedent(FIXTURE))
    res = mew_cli(
        "run",
        "--stdin",
        "--min-time",
        "1x",
        "--format",
        "json",
        stdin="bench_a.py::bench_two[n=1]\nbench_b.py::bench_two[n=2]\n",
        cwd=tmp_path,
    )
    assert res.returncode == 0, res.stderr
    rows = json.loads(res.stdout)["benchmarks"]
    assert [(r["benchmark"], r["label"]) for r in rows] == [
        ("bench_a.py::bench_two[n=1]", "n=1"),
        ("bench_b.py::bench_two[n=2]", "n=2"),
    ]


def test_directory_selectors_keep_filters_scoped_with_global_pattern(mew_cli, tmp_path):
    for directory in ("a", "b"):
        nested = tmp_path / directory / "nested"
        nested.mkdir(parents=True)
        (nested / "bench_fixture.py").write_text(textwrap.dedent(FIXTURE))
    res = mew_cli("list", "a::bench_one", "b::bench_two", "-k", "bench_two", cwd=tmp_path)
    assert res.returncode == 0, res.stderr
    assert res.stdout.splitlines() == [str(Path("b/nested/bench_fixture.py::bench_two"))]


def test_e2e_benchmark_output_cannot_corrupt_json_on_stdout(tmp_path):
    (tmp_path / "bench_noisy.py").write_text(
        textwrap.dedent(
            """
            import os

            import mew

            @mew.benchmark(iterations=1)
            def noisy(state):
                print("from print")
                os.write(1, b"from fd 1\\n")
                for _ in state:
                    pass
            """
        )
    )
    res = _mew("run", "bench_noisy.py", "-o", "-", "--format", "json", cwd=tmp_path)
    assert res.returncode == 0, res.stderr
    assert len(json.loads(res.stdout)["benchmarks"]) == 1
    assert "from print" in res.stderr and "from fd 1" in res.stderr

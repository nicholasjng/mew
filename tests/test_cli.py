"""CLI behavior, driven in-process via `mew.cli.main` for speed.

The end-to-end test at the bottom uses a real subprocess: only a fresh
interpreter proves the entry point and real file descriptors.
"""

from __future__ import annotations

import gzip
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

import mew

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

    @mew.benchmark
    def bench_untagged(state):
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

    # Output assertions expect plain text unless a test opts into color, and a
    # fixed width rather than that of the terminal pytest runs in.
    for var in ("FORCE_COLOR", "NO_COLOR", "PYTHON_COLORS"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("COLUMNS", "200")

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


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        # A parametrized family lists as one row without --show-cases.
        ((), ["bench_one", "bench_two", "bench_untagged"]),
        (("--show-cases",), ["bench_one", "bench_two[n=1]", "bench_two[n=2]", "bench_untagged"]),
        (("--show-tags",), ["bench_one\t[io]", "bench_two\t[cpu]", "bench_untagged\t[-]"]),
        (("-k", "nonexistent"), []),
    ],
)
def test_list_selects_and_expands(mew_cli, benchdir, tmp_path, args, expected):
    res = mew_cli("list", str(benchdir), "--names-only", *args, cwd=tmp_path)
    assert res.returncode == (0 if expected else 1), res.stderr
    assert res.stdout.splitlines() == expected


def _listed(entries: list[mew.Entry]) -> list[str]:
    """Name entries as `mew list --names-only` does: a narrowed family by its cases."""
    out = []
    for e in entries:
        name, labels = e.name.rsplit("::", 1)[-1], e.case_labels or []
        out += [name] if e.cases is None else [f"{name}[{labels[i]}]" for i in e.cases]
    return out


@pytest.mark.parametrize(
    ("pattern", "literal", "tags", "expected"),
    [
        ("bench_(one|untagged)", False, [], ["bench_one", "bench_untagged"]),
        ("n=2", False, [], ["bench_two[n=2]"]),  # a case-only match narrows the family
        # A displayed name, brackets and all, needs -F: a regex reads them as a class.
        ("bench_two[n=2]", True, [], ["bench_two[n=2]"]),
        ("bench_two[n=2]", False, [], []),
        # Tags are OR, and the untagged benchmark matches none; -k narrows them.
        (None, False, ["io", "cpu"], ["bench_one", "bench_two"]),
        ("bench_(one|two)", False, ["cpu"], ["bench_two"]),
    ],
)
def test_cli_selects_like_registry_filter(
    mew_cli, benchdir, tmp_path, pattern, literal, tags, expected
):
    args = (["-k", pattern] if pattern else []) + (["-F"] if literal else [])
    args += [arg for tag in tags for arg in ("-t", tag)]
    res = mew_cli("list", str(benchdir), "--names-only", *args, cwd=tmp_path)
    assert res.stdout.splitlines() == expected, res.stderr
    # Registry.filter selects the same from the suite the CLI just registered.
    assert _listed(mew.REGISTRY.filter(pattern, tags=tags, literal=literal)) == expected


def test_invalid_filter_fails_before_any_import(mew_cli, tmp_path):
    (tmp_path / "bench_x.py").write_text("print('imported')\n")
    res = mew_cli("list", "-k", "bench(", "bench_x.py", cwd=tmp_path)
    assert res.returncode == 2
    assert res.stderr.startswith("mew list: invalid benchmark filter pattern 'bench('")
    assert "imported" not in res.stdout


def test_run_summary_only_leaves_the_repetitions_to_files(mew_cli, benchdir, tmp_path):
    out = tmp_path / "rows.jsonl"
    res = mew_cli(
        "run",
        str(benchdir),
        "--min-time",
        "1x",
        "--repetitions",
        "2",
        "--summary-only",
        "-o",
        str(out),
        "-o",
        "-",
        cwd=tmp_path,
    )
    assert res.returncode == 0, res.stderr
    # The console sums up the four benchmarks; the file keeps each repetition.
    assert "Iters" not in res.stdout
    assert "Median of repetitions" in res.stdout
    assert len(out.read_text().splitlines()) == 4 * 2


def test_run_sizes_the_console_name_column_to_the_longest_row(mew_cli, benchdir, tmp_path):
    res = mew_cli("run", str(benchdir), "--min-time", "1x", cwd=tmp_path)
    assert res.returncode == 0, res.stderr
    names = [line.split(" │ ")[0] for line in res.stdout.splitlines()[1:] if " │ " in line]
    assert len(names[0]) == max(len(name.rstrip()) for name in names[1:])


@pytest.mark.parametrize("args", [("-h",), ("run", "-h")])
def test_help_is_plain_terminal_text(mew_cli, tmp_path, monkeypatch, args):
    # On 3.14+, argparse would color redirected help under PYTHON_COLORS itself.
    monkeypatch.setenv("PYTHON_COLORS", "1")
    res = mew_cli(*args, cwd=tmp_path)
    assert res.returncode == 0
    assert "usage: mew" in res.stdout
    assert "\x1b[" not in res.stdout


def test_help_honors_force_color(mew_cli, tmp_path, monkeypatch):
    # Like the reporters and tables: FORCE_COLOR colors even redirected output.
    from mew._console import sgr

    monkeypatch.setenv("FORCE_COLOR", "1")
    res = mew_cli("run", "-h", cwd=tmp_path)
    assert res.returncode == 0
    # mew's styling, not the theme argparse colors help with on 3.14+.
    assert sgr("--min-time", "cyan") in res.stdout


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


def test_list_works_from_subdirectory(mew_cli, benchdir, tmp_path):
    # Config benchpaths anchor at the project root (pyproject.toml), not cwd.
    (tmp_path / "pyproject.toml").write_text("[tool.mew]\n")
    sub = tmp_path / "src" / "pkg"
    sub.mkdir(parents=True)
    res = mew_cli("list", cwd=sub)
    assert res.returncode == 0, res.stderr
    assert "bench_one" in res.stdout


def test_list_without_the_default_benchpath_finds_nothing(mew_cli, tmp_path):
    # A fresh project without benchmarks/ is "nothing found", not a usage error.
    res = mew_cli("list", cwd=tmp_path)
    assert res.returncode == 1
    assert "no benchmarks found" in res.stderr


def test_bench_file_that_fails_to_import_exits_2(mew_cli, tmp_path):
    # Its traceback says what is wrong; the exit code is 2, not "nothing matched".
    (tmp_path / "bench_bad.py").write_text(
        "import mew\n\n@mew.benchmark(repetitions=0)\ndef bench_x(state):\n    pass\n"
    )
    res = mew_cli("run", "bench_bad.py", cwd=tmp_path)
    assert res.returncode == 2
    assert "ValueError: repetitions must be >= 1, got 0" in res.stderr
    last = res.stderr.splitlines()[-1]
    assert last.startswith("mew run: cannot import ") and last.endswith("bench_bad.py")


def test_strict_run_of_threaded_benchmarks_on_a_gil_build_exits_2(mew_cli, tmp_path, monkeypatch):
    monkeypatch.setattr("mew.runner.gil_enabled", lambda: True)
    (tmp_path / "bench_t.py").write_text(
        "import mew\n\n@mew.benchmark(threads=2)\ndef bench_t(state):\n    for _ in state:\n"
        "        pass\n"
    )
    res = mew_cli("run", "--strict", "bench_t.py", cwd=tmp_path)
    assert res.returncode == 2
    assert res.stderr.startswith("mew run: threaded benchmarks require")


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (("list", "nonexistent_dir"), "path does not exist"),
        (("run", "-o", "results.txt"), "unsupported output format"),
        (("run", "-o", "missing/results.json"), "output directory does not exist"),
        # Neither stdout nor a single-document JSON file can be appended to,
        # even next to a JSONL sink.
        (("run", "--append"), "--append requires"),
        (("run", "--append", "-o", "out.json"), "--append requires"),
        (("run", "--append", "-o", "out.jsonl", "-o", "out.json"), "not supported for the JSON"),
        # `--flag:` names the flag in argparse's error line; the usage line above
        # it lists every flag.
        (("run", "--min-time", "0x"), "--min-time: invalid --min-time '0x'"),
        (("run", "--min-time", "nan"), "--min-time: invalid --min-time 'nan'"),
        (("run", "--min-warmup-time", "-1"), "--min-warmup-time: invalid --min-warmup-time"),
        (("run", "--min-warmup-time", "x"), "--min-warmup-time: invalid --min-warmup-time"),
        (("run", "--repetitions", "0"), "--repetitions: expected a positive integer"),
        (("run", "--memory-iterations", "0"), "--memory-iterations: expected a positive integer"),
        (("run", "--sample-interval", "0"), "--sample-interval: expected a positive finite"),
        (("run", "--sample-top-k", "0"), "--sample-top-k: expected a positive integer"),
        # Checked before reading the result files, which don't exist here. The
        # `=` keeps argparse from taking `-1%` for an option.
        (("compare", "a.json", "b.json", "--statistic", "bogus"), "statistic 'bogus'"),
        (("compare", "a.json", "b.json", "--statistic", "p101"), "statistic 'p101'"),
        (("compare", "a.json", "b.json", "--regression-threshold=5"), "like '5%', got '5'"),
        (("compare", "a.json", "b.json", "--regression-threshold=-1%"), "got '-1%'"),
        (("compare", "a.json", "b.json", "--regression-threshold=nan%"), "got 'nan%'"),
        (("compare", "a.json", "b.json", "--regression-threshold=inf%"), "got 'inf%'"),
        # What the library raises, as one line rather than a traceback.
        (("compare", "a.json"), "mew compare: expected at least two result files"),
        (("compare", "a.json", "b.json", "-m", "bogus"), "mew compare: unknown metric 'bogus'"),
        (("compare", "a.json", "b.json"), "mew compare: result file not found: b.json"),
        (("sessions", "a.json"), "mew sessions: result file not found: a.json"),
        # `@latest` selects the newest session, so no session may carry the tag.
        (("run", "--min-time", "1x", "--session-tag", "latest"), "mew run: session_tag 'latest'"),
    ],
)
def test_usage_errors_exit_2(mew_cli, benchdir, args, message):
    # 2, not the "nothing matched" code 1. Run from the project root, so the
    # sink checks after discovering benchmarks/ are reached too.
    res = mew_cli(*args, cwd=benchdir.parent)
    assert res.returncode == 2
    assert message in res.stderr


@pytest.mark.parametrize(
    ("config", "args", "message"),
    [
        # The message names the file.
        ("[tool.mew]\nbenchpaths = 42\n", ("list",), "pyproject.toml: [tool.mew] benchpaths"),
        ('[tool.mew]\nsetup = "nope.py"\n', ("run",), "setup file not found"),
        # Read before the result files, which don't exist here.
        (
            '[[tool.mew.regressions.allow]]\npattern = "b"\nignore = true\n',
            ("compare", "a.json", "b.json", "--regression-threshold", "5%"),
            "'reason' is required",
        ),
    ],
)
def test_config_errors_exit_2(mew_cli, tmp_path, config, args, message):
    (tmp_path / "pyproject.toml").write_text(config)
    res = mew_cli(*args, cwd=tmp_path)
    assert res.returncode == 2
    assert message in res.stderr


@pytest.mark.parametrize(
    ("value", "seconds"), [("0.5", 0.5), ("200ms", 0.2), ("10s", 10.0), ("1m", 60.0)]
)
def test_min_warmup_time_parses_durations(value, seconds):
    from mew.cli import _warmup_seconds

    assert _warmup_seconds(value) == seconds


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
    # 4 benchmarks x 2 repetitions, of one iteration each.
    assert [b["iterations"] for b in json.loads(out.read_text())["benchmarks"]] == [1] * 8


def test_run_stdin_show_cases_selects_one_case_literally(mew_cli, benchdir):
    # A `name[label]` line from --show-cases is matched literally — no -F, no
    # bracket escaping — so exactly that one case runs.
    listing = mew_cli("list", ".", "--show-cases", "-k", "n=2", cwd=benchdir)
    res = mew_cli(
        "run", "--stdin", "--min-time", "1x", "--format", "json", stdin=listing.stdout, cwd=benchdir
    )
    assert res.returncode == 0, res.stderr
    (row,) = json.loads(res.stdout)["benchmarks"]
    assert row["benchmark"] == "bench_fixture.py::bench_two[n=2]"


def test_run_stdin_empty_runs_nothing(mew_cli, benchdir):
    # Empty stdin selects nothing — it does not fall back to benchpaths.
    res = mew_cli("run", "--stdin", "--min-time", "1x", stdin="", cwd=benchdir)
    assert res.returncode == 1
    assert "no benchmarks found" in res.stderr


def test_run_stdin_names_only_round_trip_is_cwd_independent(mew_cli, benchdir, tmp_path):
    # A path-free name selects against run's own discovery, so the run cwd
    # need not match the bench dir.
    listing = mew_cli("list", str(benchdir), "--names-only", "-k", "bench_one", cwd=tmp_path)
    res = mew_cli(
        "run",
        str(benchdir),
        "--stdin",
        "--min-time",
        "1x",
        "--format",
        "json",
        stdin=listing.stdout,
        cwd=tmp_path,  # cwd != benchdir
    )
    assert res.returncode == 0, res.stderr
    (row,) = json.loads(res.stdout)["benchmarks"]
    assert row["benchmark"].endswith("::bench_one")


def test_run_format_jsonl_streams_every_row_to_stdout_and_files(mew_cli, benchdir, tmp_path):
    # Every stdout line is a row (no console banner), so it pipes into `jq`.
    out = tmp_path / "results.jsonl.gz"
    res = mew_cli(
        "run",
        str(benchdir),
        "--min-time",
        "1x",
        "--format",
        "jsonl",
        "-o",
        "stdout",
        "-o",
        str(out),
        cwd=tmp_path,
    )
    assert res.returncode == 0, res.stderr
    names = [json.loads(line)["name"] for line in res.stdout.splitlines()]
    with gzip.open(out, "rt") as fh:
        assert [json.loads(line)["name"] for line in fh] == names
    assert len(names) == 4


def test_run_format_without_stdout_sink_warns(mew_cli, benchdir, tmp_path):
    # --format only configures stdout; with file-only sinks it has nothing to do.
    out = tmp_path / "r.json"
    res = mew_cli(
        "run", str(benchdir), "--min-time", "1x", "--format", "jsonl", "-o", str(out), cwd=tmp_path
    )
    assert res.returncode == 0, res.stderr
    assert "no effect without a stdout sink" in res.stderr
    assert res.stdout.strip() == ""


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


# Each body holds only what its profiler measures: a buffer next to `spin` would
# compete with it for CPU self time on a loaded machine.
_SAMPLED = """
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
"""

_ALLOCATING = """
    import mew

    @mew.benchmark
    def bench_hot(state):
        for _ in state:
            buf = bytearray(1 << 20)
            del buf
"""

# A sampler starved by a loaded machine can catch nothing in fewer iterations,
# leaving the row without a `top_function`.
_SAMPLED_ITERATIONS = "200x"


def test_run_flamegraph_renders_the_run_and_annotates_rows(mew_cli, tmp_path):
    pytest.importorskip("memray")
    from mew._format import fmt_bytes

    (tmp_path / "bench_profiled.py").write_text(textwrap.dedent(_ALLOCATING))
    report, out = tmp_path / "flame.html", tmp_path / "rows.json"
    res = mew_cli(
        "run",
        "bench_profiled.py",
        "--min-time",
        "20x",
        "--flamegraph",
        str(report),
        "-o",
        "-",
        "-o",
        str(out),
        cwd=tmp_path,
    )
    assert res.returncode == 0, res.stderr
    assert "bench_hot" in report.read_text()
    # The row carries the profile, and the console table shows it.
    (row,) = json.loads(out.read_text())["benchmarks"]
    assert row["memory"]["peak_bytes"] >= 1 << 20  # the loop's 1 MiB buffer
    assert fmt_bytes(row["memory"]["peak_bytes"]) in res.stdout


@pytest.mark.skipif(
    sys.version_info < (3, 15) and not mew.gil_enabled(),
    reason="pyinstrument's native sampler enables the GIL",
)
def test_run_sample_html_renders_the_run_and_annotates_rows(mew_cli, tmp_path):
    pytest.importorskip("profiling.sampling" if sys.version_info >= (3, 15) else "pyinstrument")
    from mew.cpu import reads_instructions

    (tmp_path / "bench_profiled.py").write_text(textwrap.dedent(_SAMPLED))
    report, out = tmp_path / "profile.html", tmp_path / "rows.json"
    res = mew_cli(
        "run",
        "bench_profiled.py",
        "--min-time",
        _SAMPLED_ITERATIONS,
        "--sample-interval",
        "1e-5",
        "--sample-top-k",
        "2",
        "--sample-html",
        str(report),
        "-o",
        "-",
        "-o",
        str(out),
        cwd=tmp_path,
    )
    assert res.returncode == 0, res.stderr
    assert "bench_hot" in report.read_text()
    # The row carries the profile, and the console table shows it.
    (row,) = json.loads(out.read_text())["benchmarks"]
    top = row["cpu_profile"]["top_function"]
    assert f"{top['function']} ({top['file']}" in res.stdout
    # Only free-threaded Tachyon reads bytecode instructions, at most --sample-top-k.
    instructions = row["cpu_profile"]["top_instructions"]
    assert bool(instructions) is reads_instructions()
    assert len(instructions) <= 2


@pytest.mark.skipif(sys.version_info >= (3, 15), reason="Tachyon runs here")
def test_run_rejects_tachyon_before_python_3_15(mew_cli, benchdir, tmp_path):
    res = mew_cli("run", str(benchdir), "--sample", "--sampler", "tachyon", cwd=tmp_path)
    assert res.returncode == 2
    assert "mew run: Tachyon CPU sampling requires Python 3.15" in res.stderr


@pytest.mark.parametrize(
    ("flag", "factory", "error"),
    [
        ("--sample", "mew.cpu.manager", RuntimeError),  # a sampler that can't run here
        ("--sample", "mew.cpu.manager", ImportError),  # its package is missing
        ("--sample", "mew.cpu.manager", ValueError),  # a bad setting for it
        ("--profile-memory", "mew.memory.manager", ImportError),
    ],
)
def test_run_unusable_profiler_exits_2(
    mew_cli, benchdir, tmp_path, monkeypatch, flag, factory, error
):
    # An environment error, not the "nothing matched" code 1, and before any run.
    def unusable(*args, **kwargs):
        raise error("unusable here")

    monkeypatch.setattr(factory, unusable)
    res = mew_cli("run", str(benchdir), flag, cwd=tmp_path)
    assert (res.returncode, res.stdout, res.stderr) == (2, "", "mew run: unusable here\n")


@pytest.mark.skipif(sys.version_info < (3, 15), reason="pyinstrument is the default here")
@pytest.mark.skipif(
    not mew.gil_enabled(),
    reason="pyinstrument's native sampler enables the GIL",
)
def test_run_samples_with_pyinstrument_on_python_3_15(mew_cli, tmp_path):
    """One sampler across a suite's interpreter range: pyinstrument on 3.15 too."""
    pytest.importorskip("pyinstrument")
    (tmp_path / "bench_profiled.py").write_text(textwrap.dedent(_SAMPLED))
    out = tmp_path / "rows.json"
    res = mew_cli(
        "run",
        "bench_profiled.py",
        "--min-time",
        "1x",  # a row gets a profile, sampled or not
        "--sample",
        "--sampler",
        "pyinstrument",
        "--sample-top-k",
        "2",
        "-o",
        str(out),
        cwd=tmp_path,
    )
    assert res.returncode == 0, res.stderr
    (row,) = json.loads(out.read_text())["benchmarks"]
    assert row["cpu_profile"]["profiler"] == "pyinstrument"
    # pyinstrument reads no instructions, whatever --sample-top-k says.
    assert row["cpu_profile"]["top_instructions"] == []


# --- mew compare: --regression-threshold / --exit-non-zero-on-regression ---


@pytest.mark.parametrize(
    ("flags", "code"),
    [
        # Without --exit-non-zero-on-regression the panel is informational, not a gate.
        (("--regression-threshold", "5%"), 0),
        # 3, apart from 1 (nothing to compare) and 2 (usage errors).
        (("--regression-threshold", "5%", "--exit-non-zero-on-regression"), 3),
        # The gate flag alone gates at the default threshold rather than no-opping.
        (("--exit-non-zero-on-regression",), 3),
    ],
)
def test_compare_exit_non_zero_on_regression_gates(mew_cli, tmp_path, flags, code):
    # +20%, well over both the 5% threshold and the 5% default:
    other, base = _write_pair(tmp_path, other=[_row("b", 120.0)], base=[_row("b", 100.0)])
    res = mew_cli("compare", str(other), str(base), *flags, cwd=tmp_path)
    assert res.returncode == code, res.stderr
    assert "❌" in res.stderr


def test_compare_regression_threshold_wins_over_the_configured_default(mew_cli, tmp_path):
    # +20%: within the file's 50% default, over the flag's 5%.
    other, base = _write_pair(tmp_path, other=[_row("b", 120.0)], base=[_row("b", 100.0)])
    (tmp_path / "pyproject.toml").write_text("[tool.mew.regressions]\ndefault-threshold = 50.0\n")
    args = ("compare", str(other), str(base), "--exit-non-zero-on-regression")
    assert mew_cli(*args, cwd=tmp_path).returncode == 0
    res = mew_cli(*args, "--regression-threshold", "5%", cwd=tmp_path)
    assert res.returncode == 3, res.stderr


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


@pytest.mark.parametrize(
    ("shell", "marker"),
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
    assert "rich json jsonl" in res.stdout  # --format's choices
    # Help text appears as argparse renders it, with "%%" unescaped.
    assert "%%" not in res.stdout


def test_completions_zsh_repeatable_options_star_form(mew_cli, tmp_path):
    zsh = mew_cli("completions", "zsh", cwd=tmp_path).stdout
    # Repeatable -t/--tag: `*` prefix and braces outside the quotes; no
    # self-exclusion that would stop zsh offering it a second time.
    assert "'*'{-t,--tag}'" in zsh
    assert "(-t --tag)" not in zsh
    # Non-repeatable multi-flag options keep the exclusion group.
    assert "'(-k --pattern)'{-k,--pattern}'" in zsh


@pytest.mark.parametrize("shell", ["bash", "zsh", "fish"])
def test_completions_parse_in_their_shell(mew_cli, tmp_path, shell):
    if (exe := shutil.which(shell)) is None:
        pytest.skip(f"{shell} not installed")
    script = tmp_path / f"mew.{shell}"
    script.write_text(mew_cli("completions", shell, cwd=tmp_path).stdout)
    assert subprocess.run([exe, "-n", str(script)]).returncode == 0  # parse only


def test_setup_file_context_applies_to_a_single_file_run(mew_cli, benchdir, tmp_path):
    """Setup-file context must not depend on which benchmark files are selected."""
    (tmp_path / "pyproject.toml").write_text('[tool.mew]\nsetup = "benchmarks/conf.py"\n')
    (benchdir / "conf.py").write_text('import mew\n\nmew.set_context("team", "perf")\n')
    out = tmp_path / "one.json"
    res = mew_cli(
        "run", "benchmarks/bench_fixture.py", "--min-time=1x", "-o", str(out), cwd=tmp_path
    )
    assert res.returncode == 0, res.stderr
    assert json.loads(out.read_text())["context"]["team"] == "perf"


@pytest.mark.parametrize(
    ("second", "expected"),
    [
        ("bench_b.py::bench_two", {"bench_b.py::bench_two"}),
        # An unfiltered file selects all of itself, and nothing more of a.py.
        (
            "bench_b.py",
            {"bench_b.py::bench_one", "bench_b.py::bench_two", "bench_b.py::bench_untagged"},
        ),
    ],
)
def test_path_filters_are_scoped_to_their_files(mew_cli, tmp_path, second, expected):
    for name in ("a", "b"):
        (tmp_path / f"bench_{name}.py").write_text(textwrap.dedent(FIXTURE))
    res = mew_cli("list", "bench_a.py::bench_one", second, cwd=tmp_path)
    assert res.returncode == 0, res.stderr
    assert set(res.stdout.splitlines()) == {"bench_a.py::bench_one", *expected}


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


# --- end-to-end: the real entry point in a subprocess -------------------------


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
    res = subprocess.run(
        [sys.executable, "-m", "mew.cli", "run", "bench_noisy.py", "-o", "-", "--format", "json"],
        cwd=tmp_path,
        capture_output=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        check=False,
    )
    assert res.returncode == 0, res.stderr
    assert len(json.loads(res.stdout)["benchmarks"]) == 1
    assert "from print" in res.stderr and "from fd 1" in res.stderr

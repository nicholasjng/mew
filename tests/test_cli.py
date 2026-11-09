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
from _helpers import (
    counter_row,
    row as _row,
    write_json as _write_json,
    write_jsonl as _write_jsonl,
    write_pair as _write_pair,
)

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
        (("-k", "bench_(one|untagged)"), ["bench_one", "bench_untagged"]),
        (("-k", "n=2"), ["bench_two[n=2]"]),  # a case-only match narrows the family
        # A displayed name, brackets and all, needs -F: a regex reads them as a class.
        (("-k", "bench_two[n=2]", "-F"), ["bench_two[n=2]"]),
        (("-k", "bench_two[n=2]"), []),
        # Tags are OR, and the untagged benchmark matches none; -k narrows them.
        (("-t", "io", "-t", "cpu"), ["bench_one", "bench_two"]),
        (("-k", "bench_(one|two)", "-t", "cpu"), ["bench_two"]),
    ],
)
def test_list_selects_and_expands(mew_cli, benchdir, tmp_path, args, expected):
    """`mew list` selects with `-k`, `-F` and `-t`, shows a family as one row, its cases with
    `--show-cases`, its tags with `--show-tags`, and exits 1 when nothing matches."""
    res = mew_cli("list", str(benchdir), *args, cwd=tmp_path)
    assert res.returncode == (0 if expected else 1), res.stderr
    assert [line.rsplit("::", 1)[-1] for line in res.stdout.splitlines()] == expected


def test_invalid_filter_fails_before_any_import(mew_cli, tmp_path):
    """An invalid `-k` regex exits 2 with an error before any bench file is imported."""
    (tmp_path / "bench_x.py").write_text("print('imported')\n")
    res = mew_cli("list", "-k", "bench(", "bench_x.py", cwd=tmp_path)
    assert res.returncode == 2
    assert res.stderr.startswith("mew list: invalid benchmark filter pattern 'bench('")
    assert "imported" not in res.stdout


def test_run_summary_only_leaves_the_repetitions_to_files(mew_cli, benchdir, tmp_path):
    """`--summary-only` prints only aggregates to the console; file sinks keep every repetition."""
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
    """The console table's name column is as wide as its longest benchmark name."""
    res = mew_cli("run", str(benchdir), "--min-time", "1x", cwd=tmp_path)
    assert res.returncode == 0, res.stderr
    names = [line.split(" │ ")[0] for line in res.stdout.splitlines()[1:] if " │ " in line]
    assert len(names[0]) == max(len(name.rstrip()) for name in names[1:])


@pytest.mark.parametrize("args", [("-h",), ("run", "-h")])
def test_help_is_plain_terminal_text(mew_cli, tmp_path, monkeypatch, args):
    """`-h` output contains no ANSI escapes, even with `PYTHON_COLORS=1`."""
    # On 3.14+, argparse would color redirected help under PYTHON_COLORS itself.
    monkeypatch.setenv("PYTHON_COLORS", "1")
    res = mew_cli(*args, cwd=tmp_path)
    assert res.returncode == 0
    assert "usage: mew" in res.stdout
    assert "\x1b[" not in res.stdout


def test_help_honors_force_color(mew_cli, tmp_path, monkeypatch):
    """`FORCE_COLOR` colors `-h` output with mew's own flag styling."""
    # Like the reporters and tables: FORCE_COLOR colors even redirected output.
    from mew._console import sgr

    monkeypatch.setenv("FORCE_COLOR", "1")
    res = mew_cli("run", "-h", cwd=tmp_path)
    assert res.returncode == 0
    # mew's styling, not the theme argparse colors help with on 3.14+.
    assert sgr("--min-time", "cyan") in res.stdout


def test_list_selects_symlinked_bench_file_in_benchpath(mew_cli, tmp_path):
    """`mew list` selects a symlinked bench file in the benchpath whose target lies outside it."""
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
    """`mew list` in a subdirectory finds benchmarks relative to the project root."""
    # Config benchpaths anchor at the project root (pyproject.toml), not cwd.
    (tmp_path / "pyproject.toml").write_text("[tool.mew]\n")
    sub = tmp_path / "src" / "pkg"
    sub.mkdir(parents=True)
    res = mew_cli("list", cwd=sub)
    assert res.returncode == 0, res.stderr
    assert "bench_one" in res.stdout


def test_list_without_the_default_benchpath_finds_nothing(mew_cli, tmp_path):
    """Without a `benchmarks/` directory, `mew list` reports no benchmarks found and exits 1."""
    # A fresh project without benchmarks/ is "nothing found", not a usage error.
    res = mew_cli("list", cwd=tmp_path)
    assert res.returncode == 1
    assert "no benchmarks found" in res.stderr


def test_bench_file_that_fails_to_import_exits_2(mew_cli, tmp_path):
    """A bench file that raises on import exits 2 with its traceback and a `cannot import` line."""
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
    """`mew run --strict` exits 2 for threaded benchmarks when the GIL is enabled."""
    monkeypatch.setattr("mew._options._gil_enabled", lambda: True)
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
        (("run", "--append", "-o", "out.jsonl", "-o", "out.json"), "--append requires"),
        (("run", "-o", "a.json", "-o", "./a.json"), "duplicate output sink: ./a.json"),
        # `--flag:` names the flag in argparse's error line; the usage line above
        # it lists every flag.
        (("run", "--min-time", "0x"), "--min-time: invalid --min-time '0x'"),
        (("run", "--min-warmup-time", "-1"), "--min-warmup-time: invalid --min-warmup-time"),
        (("run", "--repetitions", "0"), "--repetitions: expected a positive integer"),
        # Checked before reading the result files, which don't exist here. The
        # `=` keeps argparse from taking `-1%` for an option.
        (("compare", "a.json", "b.json", "--regression-threshold=5"), "like '5%', got '5'"),
        (("compare", "a.json", "b.json", "--regression-threshold=-1%"), "got '-1%'"),
        (("compare", "a.json", "b.json", "--regression-threshold=nan%"), "got 'nan%'"),
        # What the library raises, as one line rather than a traceback.
        (("compare", "a.json"), "mew compare: expected at least two result files"),
        (("compare", "a.json", "b.json"), "mew compare: result file not found: a.json"),
        (("log", "a.json"), "mew log: result file not found: a.json"),
        # `@latest` selects the newest run, so no run may carry that name.
        (("run", "--min-time", "1x", "--run-name", "latest"), "mew run: run_name 'latest'"),
        # Prefixes of long options are not expanded, so a new option can't break them.
        (("run", "--rep", "3"), "unrecognized arguments: --rep"),
        # Checked before the run, not after it.
        (
            ("run", "--cpu-profiler", "sample", "--profile-dir", "benchmarks/bench_fixture.py"),
            # The OS's separators, as the user's shell shows paths.
            f"mew run: --profile-dir {Path('benchmarks/bench_fixture.py')} is not a directory",
        ),
    ],
)
def test_usage_errors_exit_2(mew_cli, benchdir, args, message):
    """Invalid arguments to `list`, `run`, `compare` and `log` exit 2 with a message."""
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
            '[[tool.mew.regressions.allow]]\npattern = "b"\nthreshold = inf\n',
            ("compare", "a.json", "b.json", "--regression-threshold", "5%"),
            "'reason' is required",
        ),
    ],
)
def test_config_errors_exit_2(mew_cli, tmp_path, config, args, message):
    """An invalid `[tool.mew]` configuration exits 2 with a message on stderr."""
    (tmp_path / "pyproject.toml").write_text(config)
    res = mew_cli(*args, cwd=tmp_path)
    assert res.returncode == 2
    assert message in res.stderr


def test_run_promoted_gb_flags_accepted(mew_cli, benchdir, tmp_path):
    """`run`'s promoted Google Benchmark flags reach GB as valid flags and take effect."""
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
    """A `name[label]` line from `list --show-cases` on `run --stdin` runs exactly that case."""
    # A `name[label]` line from --show-cases is matched literally — no -F, no
    # bracket escaping — so exactly that one case runs.
    listing = mew_cli("list", ".", "--show-cases", "-k", "n=2", cwd=benchdir)
    res = mew_cli(
        "run", "--stdin", "--min-time", "1x", "--format", "json", stdin=listing.stdout, cwd=benchdir
    )
    assert res.returncode == 0, res.stderr
    (row,) = json.loads(res.stdout)["benchmarks"]
    assert row["name"] == "bench_fixture.py::bench_two[n=2]"


def test_run_stdin_empty_runs_nothing(mew_cli, benchdir):
    """`run --stdin` with empty input selects nothing and exits 1 instead of using benchpaths."""
    # Empty stdin selects nothing — it does not fall back to benchpaths.
    res = mew_cli("run", "--stdin", "--min-time", "1x", stdin="", cwd=benchdir)
    assert res.returncode == 1
    assert "no benchmarks found" in res.stderr


def test_run_format_jsonl_streams_every_row_to_stdout_and_files(mew_cli, benchdir, tmp_path):
    """`--format jsonl` writes one row per stdout line, the same rows as a gzipped JSONL file."""
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
        "-",
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
    """`--format` with only file sinks warns that it has no effect and prints nothing to stdout."""
    # --format only configures stdout; with file-only sinks it has nothing to do.
    out = tmp_path / "r.json"
    res = mew_cli(
        "run", str(benchdir), "--min-time", "1x", "--format", "jsonl", "-o", str(out), cwd=tmp_path
    )
    assert res.returncode == 0, res.stderr
    assert "no effect without a stdout sink" in res.stderr
    assert res.stdout.strip() == ""


@pytest.mark.parametrize(("flag", "captured"), [(None, True), ("no", False)])
def test_capture_flag(monkeypatch, capfd: pytest.CaptureFixture[str], tmp_path, flag, captured):
    """Benchmark output is captured and printed after the run by default; `--capture no` shows
    it live."""
    # `main()` directly, not `mew_cli`: uncaptured, the body writes to the real
    # fd 2, which only capfd sees (and capfd cannot share a test with capsys).
    from mew.cli import main

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


def test_run_profile_memray_renders_the_run_and_annotates_rows(mew_cli, tmp_path):
    """`--memory-profiler memray --profile-dir` writes a memray report per benchmark to
    `<dir>/memory`, and the rows and console table show peak memory."""
    pytest.importorskip("memray")
    from mew._console import fmt_bytes

    (tmp_path / "bench_profiled.py").write_text(textwrap.dedent(_ALLOCATING))
    reports, out = tmp_path / "flames", tmp_path / "rows.json"
    res = mew_cli(
        "run",
        "bench_profiled.py",
        "--min-time",
        "20x",
        "--memory-profiler",
        "memray",
        "--profile-dir",
        str(reports),
        "-o",
        "-",
        "-o",
        str(out),
        cwd=tmp_path,
    )
    assert res.returncode == 0, res.stderr
    # Numbered in run order, named after the function; the header names the benchmark.
    assert "bench_profiled.py::bench_hot" in (reports / "memory" / "0-bench_hot.html").read_text()
    assert not (reports / "cpu").exists()
    # The row carries the profile, and the console table shows it.
    (row,) = json.loads(out.read_text())["benchmarks"]
    assert row["memory"]["peak_bytes"] >= 1 << 20  # the loop's 1 MiB buffer
    assert fmt_bytes(row["memory"]["peak_bytes"]) in res.stdout


@pytest.mark.skipif(
    sys.version_info < (3, 15) and not mew.machine._gil_enabled(),
    reason="pyinstrument's native sampler enables the GIL",
)
def test_run_sample_renders_the_run_and_annotates_rows(mew_cli, tmp_path):
    """A preset from `[tool.mew.profilers]` samples the run: `--profile-dir` gets its report in
    `<dir>/cpu`, the rows and console table the hottest function, and the context the
    preset that ran."""
    pytest.importorskip("profiling.sampling" if sys.version_info >= (3, 15) else "pyinstrument")

    (tmp_path / "bench_profiled.py").write_text(textwrap.dedent(_SAMPLED))
    sampler = "TachyonManager" if sys.version_info >= (3, 15) else "PyinstrumentManager"
    (tmp_path / "pyproject.toml").write_text(
        f'[tool.mew.profilers.fine]\nclass = "mew.cpu:{sampler}"\ninterval = 1e-5\n'
    )
    reports, out = tmp_path / "profiles", tmp_path / "rows.json"
    res = mew_cli(
        "run",
        "bench_profiled.py",
        "--min-time",
        _SAMPLED_ITERATIONS,
        "--cpu-profiler",
        "fine",
        "--profile-dir",
        str(reports),
        "-o",
        "-",
        "-o",
        str(out),
        cwd=tmp_path,
    )
    assert res.returncode == 0, res.stderr
    assert "bench_hot" in (reports / "cpu" / "index.html").read_text()
    # The row carries the profile, and the console table shows it.
    (row,) = json.loads(out.read_text())["benchmarks"]
    top = row["cpu_profile"]["top_function"]
    assert f"{top['function']} ({top['file']}" in res.stdout
    assert row["context"]["profilers"] == {
        "cpu": {"preset": "fine", "class": f"mew.cpu:{sampler}", "options": {"interval": 1e-5}}
    }


def test_run_profile_dir_needs_a_profiler(mew_cli, benchdir, tmp_path):
    """`--profile-dir` alone exits 2 before running anything: no manager would write to it."""
    res = mew_cli("run", str(benchdir), "--profile-dir", str(tmp_path / "out"), cwd=tmp_path)
    assert res.returncode == 2
    assert "--profile-dir needs --cpu-profiler or --memory-profiler" in res.stderr
    assert not (tmp_path / "out").exists()


_USER_MEMORY_MANAGER = """
import mew

class Fixed(mew.MemoryManager):
    def __init__(self, peak, *, tag):
        self.peak, self.tag, self.passes = peak, tag, 0

    def start(self):
        pass

    def stop(self):
        self.passes += 1
        return {"peak_bytes": self.peak, "total_allocations": 1}

    def write_outputs(self, directory):
        directory.mkdir(parents=True)
        (directory / "passes.txt").write_text(f"{self.tag}:{self.passes}")
"""


def test_run_profiles_with_a_user_preset(mew_cli, benchdir, tmp_path):
    """A project's preset fills the rows' memory block, the context records it, and
    `--profile-dir` gets the files of the manager that ran."""
    (tmp_path / "profilers.py").write_text(_USER_MEMORY_MANAGER)
    (tmp_path / "pyproject.toml").write_text(
        '[tool.mew.profilers.fixed]\nclass = "profilers.py:Fixed"\npeak = 42\ntag = "hi"\n'
    )
    out, profiles = tmp_path / "rows.json", tmp_path / "profiles"
    res = mew_cli(
        "run", str(benchdir), "-k", "bench_one", "--min-time", "2x", "--memory-profiler", "fixed",
        "--profile-dir", str(profiles), "-o", str(out), cwd=tmp_path,
    )  # fmt: skip
    assert res.returncode == 0, res.stderr
    (row,) = json.loads(out.read_text())["benchmarks"]
    assert row["memory"]["peak_bytes"] == 42
    record = {
        "preset": "fixed",
        "class": "profilers.py:Fixed",
        "options": {"peak": 42, "tag": "hi"},
    }
    assert row["context"]["profilers"] == {"memory": record}
    assert (profiles / "memory" / "passes.txt").read_text() == "hi:1"


def test_run_profile_dir_skips_a_manager_without_write_outputs(mew_cli, benchdir, tmp_path):
    """A manager needn't subclass the protocol: without write_outputs, `--profile-dir` writes
    nothing for it and the run still succeeds."""
    (tmp_path / "prof.py").write_text(
        "class Prof:\n"
        "    def after_setup_start(self): ...\n"
        "    def before_teardown_stop(self): ...\n"
    )
    profiles = tmp_path / "profiles"
    res = mew_cli(
        "run", str(benchdir), "-k", "bench_one", "--min-time", "2x", "--cpu-profiler",
        "prof.py:Prof",
        "--profile-dir", str(profiles), cwd=tmp_path,
    )  # fmt: skip
    assert res.returncode == 0, res.stderr
    assert not (profiles / "cpu").exists()


def test_run_exits_2_on_a_missing_option(mew_cli, benchdir, tmp_path):
    """A constructor's TypeError, here a required option the preset leaves out, is a usage error."""
    (tmp_path / "profilers.py").write_text(_USER_MEMORY_MANAGER)
    res = mew_cli("run", str(benchdir), "--memory-profiler", "profilers.py:Fixed", cwd=tmp_path)
    assert res.returncode == 2
    assert "mew run: profilers.py:Fixed: " in res.stderr
    assert "Traceback" not in res.stderr


@pytest.mark.skipif(sys.version_info >= (3, 15), reason="Tachyon runs here")
def test_run_rejects_tachyon_before_python_3_15(mew_cli, benchdir, tmp_path):
    """Tachyon exits 2 on Python versions before 3.15."""
    res = mew_cli("run", str(benchdir), "--cpu-profiler", "mew.cpu:TachyonManager", cwd=tmp_path)
    assert res.returncode == 2
    assert "mew run: Tachyon CPU sampling requires Python 3.15" in res.stderr


@pytest.mark.parametrize(
    ("flag", "preset", "factory", "error"),
    [
        ("--cpu-profiler", "sample", "mew.cpu.manager", RuntimeError),  # can't run here
        ("--cpu-profiler", "sample", "mew.cpu.manager", ImportError),  # its package is missing
        ("--cpu-profiler", "sample", "mew.cpu.manager", ValueError),  # a bad setting for it
        ("--memory-profiler", "memray", "mew.memory.MemrayManager", ImportError),
    ],
)
def test_run_unusable_profiler_exits_2(
    mew_cli, benchdir, tmp_path, monkeypatch, flag, preset, factory, error
):
    """A sampler or memory profiler that fails to set up exits 2 with its error before any run."""

    # An environment error, not the "nothing matched" code 1, and before any run.
    def unusable(*args, **kwargs):
        raise error("unusable here")

    monkeypatch.setattr(factory, unusable)
    res = mew_cli("run", str(benchdir), flag, preset, cwd=tmp_path)
    assert (res.returncode, res.stdout, res.stderr) == (2, "", "mew run: unusable here\n")


@pytest.mark.skipif(sys.version_info < (3, 15), reason="pyinstrument is the default here")
@pytest.mark.skipif(
    not mew.machine._gil_enabled(),
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
        "--cpu-profiler",
        "mew.cpu:PyinstrumentManager",
        "-o",
        str(out),
        cwd=tmp_path,
    )
    assert res.returncode == 0, res.stderr
    (row,) = json.loads(out.read_text())["benchmarks"]
    assert row["cpu_profile"]["profiler"] == "pyinstrument"


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
    """A regression exits 3 only with `--exit-non-zero-on-regression`, which gates at the default
    threshold when `--regression-threshold` is absent."""
    # +20%, well over both the 5% threshold and the 5% default:
    other, base = _write_pair(tmp_path, other=[_row("b", 120.0)], base=[_row("b", 100.0)])
    res = mew_cli("compare", str(base), str(other), *flags, cwd=tmp_path)
    assert res.returncode == code, res.stderr
    assert "❌" in res.stderr


def test_compare_regression_threshold_wins_over_the_configured_default(mew_cli, tmp_path):
    """`--regression-threshold` overrides `default-threshold` from `[tool.mew.regressions]`."""
    # +20%: within the file's 50% default, over the flag's 5%.
    other, base = _write_pair(tmp_path, other=[_row("b", 120.0)], base=[_row("b", 100.0)])
    (tmp_path / "pyproject.toml").write_text("[tool.mew.regressions]\ndefault-threshold = 50.0\n")
    args = ("compare", str(base), str(other), "--exit-non-zero-on-regression")
    assert mew_cli(*args, cwd=tmp_path).returncode == 0
    res = mew_cli(*args, "--regression-threshold", "5%", cwd=tmp_path)
    assert res.returncode == 3, res.stderr


@pytest.mark.parametrize(
    ("pyproject", "code", "message"),
    [
        # No rule, no direction: a usage error.
        ("", 2, "no direction is declared"),
        ('[[tool.mew.counters]]\npattern = "*_misses"\nhigher-is-better = false\n', 3, "❌"),
    ],
)
def test_compare_gates_on_a_counter_in_its_configured_direction(
    mew_cli, tmp_path, pyproject, code, message
):
    """A `[[tool.mew.counters]]` rule gives a counter its direction; without one, gating fails."""
    other, base = _write_pair(
        tmp_path,
        other=[counter_row("b", "tlb_misses", 120.0)],
        base=[counter_row("b", "tlb_misses", 100.0)],
    )
    (tmp_path / "pyproject.toml").write_text(pyproject)
    args = ("-m", "counters.tlb_misses", "--exit-non-zero-on-regression")
    res = mew_cli("compare", str(base), str(other), *args, cwd=tmp_path)
    assert res.returncode == code, res.stderr
    assert message in res.stderr


@pytest.mark.parametrize(
    ("pyproject", "args", "shown", "hidden"),
    [
        ("", (), "+0.00%", "+900.00%"),  # the median of 1, 1, 10 is 1
        ("", ("-k", "bench_a"), "bench_a", "bench_b"),
    ],
)  # fmt: skip
def test_compare_options_reach_the_comparison(mew_cli, tmp_path, pyproject, args, shown, hidden):
    """`-k` changes what
    `mew compare` prints, not only what it accepts."""
    other, base = _write_pair(
        tmp_path,
        # bench_b moves +20%, so +0.00% can only be bench_a's median.
        other=[*(_row("x.py::bench_a", t) for t in (1.0, 1.0, 10.0)), _row("x.py::bench_b", 6.0)],
        base=[*(_row("x.py::bench_a", 1.0) for _ in range(3)), _row("x.py::bench_b", 5.0)],
    )
    (tmp_path / "pyproject.toml").write_text(pyproject)
    res = mew_cli("compare", str(base), str(other), *args, cwd=tmp_path)
    output = res.stdout + res.stderr
    assert shown in output
    assert hidden not in output


def test_log_lists_runs_newest_first(mew_cli, tmp_path):
    """`mew log` lists a result file's runs with their names, newest first."""
    p = tmp_path / "r.json"
    _write_json(
        p,
        [
            _row("b", 1.0, date="2026-01-01T00:00:00", run_id="aaaaaaaa-1", run_name="before"),
            _row("b", 2.0, date="2026-02-01T00:00:00", run_id="bbbbbbbb-2", run_name="after"),
        ],
    )
    res = mew_cli("log", str(p), cwd=tmp_path)
    assert res.returncode == 0, res.stderr
    lines = res.stdout.splitlines()
    assert lines[0].startswith("Id")
    assert lines[2].startswith("bbbbbbbb") and "after" in lines[2]
    assert lines[3].startswith("aaaaaaaa") and "before" in lines[3]


def test_log_ids_tell_runs_from_the_same_minute_apart(mew_cli, tmp_path):
    """UUIDv7s of runs from the same minute share 8 digits, so `mew log` shows enough to tell
    them apart, without ending on a hyphen."""
    p = tmp_path / "r.jsonl"
    ids = ["01a1bf92-b3c4-7000-8000-000000000001", "01a1bf92-b3d9-7000-8000-000000000002"]
    # 8 digits collide, and a 9th would end on the hyphen.
    _write_jsonl(
        p, [_row("b", 1.0, date=f"2026-01-01T00:00:0{i}", run_id=id_) for i, id_ in enumerate(ids)]
    )
    res = mew_cli("log", str(p), cwd=tmp_path)
    assert res.returncode == 0, res.stderr
    assert {line.split()[0] for line in res.stdout.splitlines()[2:]} == {
        "01a1bf92-b3c",
        "01a1bf92-b3d",
    }


def _two_runs(path: Path) -> None:
    """`before` with `old`, then `after` with `new` (counted) and `broken` (skipped)."""
    before = {"date": "2026-01-01T00:00:00", "run_id": "a-1", "run_name": "before"}
    after = {"date": "2026-02-01T00:00:00", "run_id": "b-2", "run_name": "after"}
    hits = [{"name": "hits", "value": 42.0}]
    _write_json(
        path,
        [
            _row("old", 1.0, **before),
            _row("new", 2.0, counters=hits, **after),
            _row("broken", 0.0, skip={"reason": "skipped", "message": "no GPU"}, **after),
        ],
    )


def test_show_prints_the_newest_run_as_mew_run_printed_it(mew_cli, tmp_path):
    """`mew show` prints the newest run's rows, counters and skips like `mew run`."""
    _two_runs(tmp_path / "r.json")
    res = mew_cli("show", "r.json", cwd=tmp_path)
    assert res.returncode == 0, res.stderr
    assert res.stdout.startswith("mew · ")
    assert "hits=42" in res.stdout
    assert "no GPU" in res.stdout
    assert "old" not in res.stdout


def test_show_picks_a_named_run_and_filters_it(mew_cli, tmp_path):
    """`file@name` picks a run and `-k` narrows its rows, as in `mew compare`."""
    _two_runs(tmp_path / "r.json")
    res = mew_cli("show", "r.json@before", cwd=tmp_path)
    assert res.returncode == 0, res.stderr
    assert "old" in res.stdout
    assert "new" not in res.stdout
    res = mew_cli("show", "r.json@after", "-k", "new", cwd=tmp_path)
    assert "new" in res.stdout
    assert "broken" not in res.stdout


@pytest.mark.parametrize(
    ("args", "code", "message"),
    [
        (("r.json", "-k", "nothing"), 1, "no benchmarks match 'nothing'"),
        (("r.json@nope",), 2, "no run named 'nope'"),
        (("missing.json",), 2, "result file not found"),
    ],
)
def test_show_errors(mew_cli, tmp_path, args, code, message):
    """No match exits 1; a bad file or selector is a usage error."""
    _two_runs(tmp_path / "r.json")
    res = mew_cli("show", *args, cwd=tmp_path)
    assert res.returncode == code
    assert message in res.stderr


def test_show_heads_each_of_several_files_with_its_name(mew_cli, tmp_path):
    """Several files print in order, each after its name; one the filter empties is left out."""
    _write_json(tmp_path / "a.json", [_row("alpha", 1.0)])
    _write_json(tmp_path / "b.json", [_row("beta", 2.0)])
    res = mew_cli("show", "a.json", "b.json", cwd=tmp_path)
    assert res.returncode == 0, res.stderr
    lines = res.stdout.splitlines()
    assert lines[0] == "a.json"
    assert lines[lines.index("b.json") - 1] == ""  # a blank line apart
    assert res.stdout.index("alpha") < res.stdout.index("b.json") < res.stdout.index("beta")
    res = mew_cli("show", "a.json", "b.json", "-k", "beta", cwd=tmp_path)
    assert res.stdout.splitlines()[0] == "b.json"
    assert "a.json" not in res.stdout


def test_show_summarizes_repetitions(mew_cli, tmp_path):
    """`--summary-only` leaves repetitions, and their counters, to the summary."""
    hits = [{"name": "hits", "value": 7.0}]
    reps = [
        _row("rep", t, repetitions=3, repetition=i, counters=hits)
        for i, t in enumerate((1.0, 2.0, 3.0))
    ]
    _write_json(tmp_path / "r.json", reps)
    res = mew_cli("show", "r.json", cwd=tmp_path)
    assert res.stdout.count("hits=7") == 3
    res = mew_cli("show", "r.json", "--summary-only", cwd=tmp_path)
    assert res.returncode == 0, res.stderr
    assert "hits=7" not in res.stdout
    assert "Median of repetitions" in res.stdout


@pytest.mark.parametrize(
    ("shell", "marker"),
    [
        ("bash", "complete -o filenames -F _mew mew"),
        ("zsh", "compdef _mew mew"),
        ("fish", "complete -c mew"),
    ],
)
def test_completions_generate(mew_cli, shell, marker, tmp_path):
    """`mew completions` emits bash, zsh and fish scripts with subcommands, options and choices."""
    res = mew_cli("completions", shell, cwd=tmp_path)
    assert res.returncode == 0, res.stderr
    assert marker in res.stdout
    # fish renders long opts as `-l pattern`, so match the stem.
    assert "pattern" in res.stdout
    assert "rich json jsonl" in res.stdout  # --format's choices
    # Help text appears as argparse renders it, with "%%" unescaped.
    assert "%%" not in res.stdout


def test_completions_zsh_repeatable_options_star_form(mew_cli, tmp_path):
    """zsh completions let `-t/--tag` repeat; other multi-flag options keep exclusion groups."""
    zsh = mew_cli("completions", "zsh", cwd=tmp_path).stdout
    # Repeatable -t/--tag: `*` prefix and braces outside the quotes; no
    # self-exclusion that would stop zsh offering it a second time.
    assert "'*'{-t,--tag}'" in zsh
    assert "(-t --tag)" not in zsh
    # Non-repeatable multi-flag options keep the exclusion group.
    assert "'(-k --pattern)'{-k,--pattern}'" in zsh


@pytest.mark.parametrize("shell", ["bash", "zsh", "fish"])
def test_completions_parse_in_their_shell(mew_cli, tmp_path, shell):
    """Each generated completion script parses without syntax errors in its own shell."""
    if (exe := shutil.which(shell)) is None:
        pytest.skip(f"{shell} not installed")
    script = tmp_path / f"mew.{shell}"
    script.write_text(mew_cli("completions", shell, cwd=tmp_path).stdout)
    assert subprocess.run([exe, "-n", str(script)]).returncode == 0  # parse only


def test_setup_file_context_hook_applies_to_a_single_file_run(mew_cli, benchdir, tmp_path):
    """The setup file's `context()` gives every run its context, whichever benchmark files are
    selected. (The CLI's own `profilers` entry is checked with the user preset above.)"""
    (tmp_path / "pyproject.toml").write_text('[tool.mew]\nsetup = "benchmarks/conf.py"\n')
    (benchdir / "conf.py").write_text('def context():\n    return {"team": "perf"}\n')
    out = tmp_path / "one.json"
    res = mew_cli(
        "run", "benchmarks/bench_fixture.py", "--min-time=1x", "-o", str(out), cwd=tmp_path
    )
    assert res.returncode == 0, res.stderr
    assert json.loads(out.read_text())["benchmarks"][0]["context"]["team"] == "perf"


@pytest.mark.parametrize(
    ("hook", "message"),
    [
        ("def context():\n    raise RuntimeError('no VCS')\n", "context() in"),
        ("def context():\n    return ['team', 'perf']\n", "must return a dict with string keys"),
    ],
)
def test_a_failing_context_hook_exits_2_before_any_benchmark(
    mew_cli, benchdir, tmp_path, hook, message
):
    """A hook that raises, or returns anything but a dict, stops `mew run` before it runs."""
    (tmp_path / "pyproject.toml").write_text('[tool.mew]\nsetup = "benchmarks/conf.py"\n')
    (benchdir / "conf.py").write_text(hook)
    out = tmp_path / "out.json"
    res = mew_cli("run", "--min-time=1x", "-o", str(out), cwd=tmp_path)
    assert res.returncode == 2
    assert message in res.stderr
    assert not out.exists()


def test_context_option_adds_to_and_overrides_the_hook(mew_cli, benchdir, tmp_path):
    """`--context key=value` records strings as given, over the setup file's `context()`: a
    later repeat wins, the value may hold `=`, and a dotted key stays one key."""
    (tmp_path / "pyproject.toml").write_text('[tool.mew]\nsetup = "benchmarks/conf.py"\n')
    (benchdir / "conf.py").write_text(
        'def context():\n    return {"engine": "from the hook", "team": "perf"}\n'
    )
    out = tmp_path / "out.json"
    args = ["--context", "engine=alpha 1.5.3", "--context", "n=8", "--context", "n=16"]
    args += ["--context", "expr=a=b", "--context", "ci.job=42"]
    res = mew_cli("run", "-k", "bench_one", "--min-time=1x", *args, "-o", str(out), cwd=tmp_path)
    assert res.returncode == 0, res.stderr
    context = json.loads(out.read_text())["benchmarks"][0]["context"]
    assert context["engine"] == "alpha 1.5.3"
    assert context["team"] == "perf"
    assert context["n"] == "16"  # a string: --context doesn't guess types
    assert context["expr"] == "a=b"
    assert context["ci.job"] == "42" and "ci" not in context


@pytest.mark.parametrize("value", ["no-equals", "=value"])
def test_context_option_needs_a_key_and_a_value(mew_cli, benchdir, tmp_path, value):
    """A `--context` without `=`, or without a key, is a usage error."""
    res = mew_cli("run", str(benchdir), "--context", value, cwd=tmp_path)
    assert res.returncode == 2
    assert "expected <key>=<value>" in res.stderr


def test_list_never_calls_the_context_hook(mew_cli, benchdir, tmp_path):
    """`mew list` imports the setup file but not its `context()`, which may shell out."""
    (tmp_path / "pyproject.toml").write_text('[tool.mew]\nsetup = "benchmarks/conf.py"\n')
    (benchdir / "conf.py").write_text("def context():\n    raise AssertionError('called')\n")
    res = mew_cli("list", cwd=tmp_path)
    assert res.returncode == 0, res.stderr
    assert "bench_one" in res.stdout


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
    """A `file::name` selector filters only its own file; a bare file selector selects all of it."""
    for name in ("a", "b"):
        (tmp_path / f"bench_{name}.py").write_text(textwrap.dedent(FIXTURE))
    res = mew_cli("list", "bench_a.py::bench_one", second, cwd=tmp_path)
    assert res.returncode == 0, res.stderr
    assert set(res.stdout.splitlines()) == {"bench_a.py::bench_one", *expected}


def test_stdin_path_selectors_do_not_cross_select_family_cases(mew_cli, tmp_path):
    """Path-qualified case lines on `run --stdin` select only that file's case of a family."""
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
    assert [(r["name"], r["label"]) for r in rows] == [
        ("bench_a.py::bench_two[n=1]", "n=1"),
        ("bench_b.py::bench_two[n=2]", "n=2"),
    ]


def test_directory_selectors_keep_filters_scoped_with_global_pattern(mew_cli, tmp_path):
    """Directory `::name` selectors keep their own filters when combined with a global `-k`."""
    for directory in ("a", "b"):
        nested = tmp_path / directory / "nested"
        nested.mkdir(parents=True)
        (nested / "bench_fixture.py").write_text(textwrap.dedent(FIXTURE))
    res = mew_cli("list", "a::bench_one", "b::bench_two", "-k", "bench_two", cwd=tmp_path)
    assert res.returncode == 0, res.stderr
    # Names use `/` on every OS.
    assert res.stdout.splitlines() == ["b/nested/bench_fixture.py::bench_two"]


# --- end-to-end: the real entry point in a subprocess -------------------------


def test_e2e_benchmark_output_cannot_corrupt_json_on_stdout(tmp_path):
    """Benchmark output from `print` and fd 1 goes to stderr, so JSON on stdout stays valid."""
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


def test_run_names_with_double_slashes_can_be_selected(mew_cli, benchdir, tmp_path):
    """`file@name` stays a string: a path would normalize the name `a//b` to `a/b`."""
    out = tmp_path / "r.jsonl"
    for tag in ("a//b", "c"):
        res = mew_cli(
            "run", str(benchdir), "-k", "bench_one", "--min-time", "1x", "--run-name", tag,
            "-o", str(out), "--append", cwd=tmp_path,
        )  # fmt: skip
        assert res.returncode == 0, res.stderr
    assert mew_cli("show", f"{out}@a//b", cwd=tmp_path).returncode == 0
    res = mew_cli("compare", f"{out}@c", f"{out}@a//b", cwd=tmp_path)
    assert res.returncode == 0, res.stderr


def test_run_leaves_the_registry_as_it_found_it(mew_cli, benchdir, tmp_path):
    """A run doesn't drop benchmarks registered before it, as in a notebook, nor leave its
    suite's benchmarks registered."""
    from mew._registry import REGISTRY

    @mew.benchmark
    def bench_mine(state):
        for _ in state:
            pass

    before = REGISTRY.all()
    res = mew_cli("run", str(benchdir), "-k", "bench_one", "--min-time", "1x", cwd=tmp_path)
    assert res.returncode == 0, res.stderr
    assert REGISTRY.all() == before


def test_usage_errors_are_colored_by_stderr_not_stdout(monkeypatch, capsys):
    """argparse prints usage errors to stderr, so a terminal on stdout doesn't color them."""
    from mew.cli import main

    for var in ("FORCE_COLOR", "NO_COLOR", "PYTHON_COLORS"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    with pytest.raises(SystemExit):
        main(["--help"])
    assert "\x1b[" in capsys.readouterr().out
    with pytest.raises(SystemExit) as exc:
        main(["run", "--bogus"])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "unrecognized arguments: --bogus" in err
    assert "\x1b[" not in err


def _bash() -> str | None:
    """bash; on Windows, Git's. There a bare `bash` finds System32's WSL launcher before
    PATH, which fails without a Linux distribution installed."""
    if sys.platform != "win32":
        return shutil.which("bash")
    git = shutil.which("git")
    if git is None:
        return None
    candidates = (p / "bin" / "bash.exe" for p in Path(git).resolve().parents)
    return next((str(c) for c in candidates if c.is_file()), None)


_BASH = _bash()


@pytest.mark.skipif(_BASH is None, reason="needs bash (Git Bash on Windows)")
def test_bash_completion_keeps_file_names_with_spaces_whole(mew_cli, tmp_path):
    """File completion splits on newlines only, while choice lists still split on spaces."""
    script = tmp_path / "mew.bash"
    # "\n" line ends on every platform: bash reads a "\r" as part of the line.
    script.write_text(mew_cli("completions", "bash", cwd=tmp_path).stdout, newline="\n")
    (tmp_path / "my benches").mkdir()
    probe = (
        # Relative, as bash runs in tmp_path: a Windows path's backslashes are escapes.
        "source ./mew.bash\n"
        't() { COMP_WORDS=("$@"); COMP_CWORD=$(( ${#COMP_WORDS[@]} - 1 )); COMPREPLY=(); _mew; '
        'printf "[%s]" "${COMPREPLY[@]}"; echo; }\n'
        't mew run my\nt mew run --format ""\n'
    )
    assert _BASH is not None  # for the type checker; skipped otherwise
    res = subprocess.run([_BASH, "-c", probe], cwd=tmp_path, capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    assert res.stdout.splitlines() == ["[my benches]", "[rich][json][jsonl]"]


def test_completions_offer_top_level_options_and_choices(mew_cli, tmp_path):
    """All three shells offer `-h/--version` before a command, and `mew completions`' shells."""
    bash = mew_cli("completions", "bash", cwd=tmp_path).stdout
    # The command-less branch: subcommands and top-level options in one word list.
    top = next(line for line in bash.splitlines() if 'compgen -W "list' in line)
    assert "--version" in top and "-h" in top
    assert 'compgen -W "bash zsh fish"' in bash
    zsh = mew_cli("completions", "zsh", cwd=tmp_path).stdout
    assert "'--version[Show version information.]'" in zsh
    fish = mew_cli("completions", "fish", cwd=tmp_path).stdout
    assert "complete -c mew -n __fish_use_subcommand -l version" in fish
    assert (
        "complete -c mew -n '__fish_seen_subcommand_from completions' -xa 'bash zsh fish'" in fish
    )


def test_benchmark_names_are_relative_to_the_project_root(mew_cli, tmp_path):
    """A benchmark's name, which `mew compare` matches by, doesn't depend on the working
    directory; two same-named files can't collide from outside the project."""
    proj = tmp_path / "proj"
    for sub in ("x", "y"):
        (proj / sub).mkdir(parents=True)
        (proj / sub / "bench_sort.py").write_text(
            "import mew\n\n@mew.benchmark\n"
            "def bench_sort(state):\n    for _ in state:\n        pass\n"
        )
    (proj / "pyproject.toml").write_text("")
    (tmp_path / "other").mkdir()
    expected = ["x/bench_sort.py::bench_sort", "y/bench_sort.py::bench_sort"]
    for cwd, target in ((proj, "."), (proj / "x", ".."), (tmp_path / "other", "../proj")):
        res = mew_cli("list", target, cwd=cwd)
        assert res.returncode == 0, res.stderr
        assert res.stdout.split() == expected  # with `/` on every OS

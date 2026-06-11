"""Out-of-process profiler backends and the subprocess worker.

The worker is exercised in a real subprocess (faithful to how the profilers
launch it, and sidesteps import_file's sys.modules caching). Backend selection,
the xctrace argv builder, and availability probing are unit-tested without
invoking any external profiler, since those need full Xcode / Linux tooling.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from mew import profilers
from mew.profilers import base
from mew.profilers.xctrace import XctraceProfiler, _record_command

_BENCH_SRC = textwrap.dedent(
    """
    import os
    import mew

    @mew.benchmark
    def bench_a(state):
        n = 0
        for _ in state:
            n += 1
        with open(os.environ["MEW_COUNT_OUT"], "w") as fh:
            fh.write(str(n))
    """
)

_FAMILY_SRC = textwrap.dedent(
    """
    import os
    import mew

    @mew.parametrize([{"n": 10}, {"n": 20}, {"n": 30}])
    def bench_fam(state, n):
        for _ in state:
            pass
        with open(os.environ["MEW_COUNT_OUT"], "a") as fh:
            fh.write(f"{n}\\n")
    """
)


def _write_bench(tmp_path: Path, src: str) -> Path:
    bench = tmp_path / "bench_x.py"
    bench.write_text(src)
    return bench


# --- worker (real subprocess) ------------------------------------------------


def test_worker_runs_selected_entry_in_subprocess(tmp_path, monkeypatch):
    bench = _write_bench(tmp_path, _BENCH_SRC)
    count_out = tmp_path / "count.txt"
    monkeypatch.setenv("MEW_COUNT_OUT", str(count_out))

    # tmp_path is outside cwd, so the registered name is `bench_x.py::bench_a`.
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "mew._subprocess_worker",
            "--file",
            str(bench),
            "--entry",
            "bench_x.py::bench_a",
            "--iterations",
            "37",
        ],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert count_out.read_text() == "37"


def test_worker_drives_family_case_by_range(tmp_path, monkeypatch):
    bench = _write_bench(tmp_path, _FAMILY_SRC)
    count_out = tmp_path / "count.txt"
    monkeypatch.setenv("MEW_COUNT_OUT", str(count_out))

    # case index 1 → the {"n": 20} variant via the family trampoline.
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "mew._subprocess_worker",
            "--file",
            str(bench),
            "--entry",
            "bench_x.py::bench_fam",
            "--case",
            "1",
            "--iterations",
            "1",
        ],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert count_out.read_text().strip() == "20"


def test_worker_unknown_entry_exits_nonzero(tmp_path):
    bench = _write_bench(tmp_path, _BENCH_SRC)
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "mew._subprocess_worker",
            "--file",
            str(bench),
            "--entry",
            "bench_x.py::does_not_exist",
        ],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 1
    assert "not found" in proc.stderr


# --- shared helpers ----------------------------------------------------------


def test_worker_argv_is_the_shared_tail():
    argv = base.worker_argv(file="/b/bench.py", entry_name="bench.py::f", case=2, iterations=9)
    assert argv[:3] == [sys.executable, "-m", "mew._subprocess_worker"]
    assert argv[argv.index("--entry") + 1] == "bench.py::f"
    assert argv[argv.index("--case") + 1] == "2"


def test_slug_is_filesystem_safe():
    assert base.slug("bench.py::f/case:0") == "bench.py-f-case-0"
    assert base.slug("///") == "bench"


# --- xctrace argv builder ----------------------------------------------------


def test_record_command_combined_appends_after_first():
    common = dict(
        template="Time Profiler",
        time_limit=None,
        file="/b/bench.py",
        entry_name="bench.py::f",
        case=0,
        iterations=1000,
    )
    first = _record_command("xctrace", dest=Path("o/mew.trace"), append=False, **common)  # ty: ignore[invalid-argument-type]
    later = _record_command("xctrace", dest=Path("o/mew.trace"), append=True, **common)  # ty: ignore[invalid-argument-type]

    assert first[:2] == ["xctrace", "record"]
    assert "--append-run" not in first
    assert "--append-run" in later
    # The worker invocation is wired through after `--launch --`.
    tail = first[first.index("--") + 1 :]
    assert tail[:3] == [sys.executable, "-m", "mew._subprocess_worker"]
    assert "bench.py::f" in tail


def test_record_command_includes_time_limit():
    cmd = _record_command(
        "xctrace",
        template="Time Profiler",
        dest=Path("o/mew.trace"),
        append=False,
        time_limit="10s",
        file="/b/bench.py",
        entry_name="bench.py::f",
        case=0,
        iterations=1000,
    )
    assert cmd[cmd.index("--time-limit") + 1] == "10s"


# --- backend selection -------------------------------------------------------


def test_select_unknown_profiler_errors():
    with pytest.raises(SystemExit, match="unknown profiler"):
        profilers.select("nope")


def test_select_named_unavailable_reports_reason(monkeypatch):
    monkeypatch.setattr(profilers.sys, "platform", "linux")
    # xctrace is macOS-only, so on linux it reports its reason rather than running.
    with pytest.raises(SystemExit, match="macOS-only"):
        profilers.select("xctrace")


def test_select_auto_picks_platform_native_when_available(monkeypatch):
    monkeypatch.setattr(profilers.sys, "platform", "darwin")
    monkeypatch.setattr(XctraceProfiler, "unavailable_reason", lambda self: None)
    backend = profilers.select("auto")
    assert backend.name == "xctrace"


def test_select_auto_errors_with_sample_hint_when_none_available(monkeypatch):
    monkeypatch.setattr(profilers.sys, "platform", "darwin")
    monkeypatch.setattr(XctraceProfiler, "unavailable_reason", lambda self: "no Xcode")
    with pytest.raises(SystemExit, match="mew run --sample"):
        profilers.select("auto")


def test_pyspy_unavailable_on_macos(monkeypatch):
    monkeypatch.setattr("mew.profilers.pyspy.sys.platform", "darwin")
    reason = profilers._BACKENDS["py-spy"].unavailable_reason()
    assert reason is not None
    assert "macOS" in reason

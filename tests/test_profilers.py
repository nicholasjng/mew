"""Out-of-process profiler backends and the subprocess worker.

The worker is exercised in a real subprocess (faithful to how the profilers
launch it, and sidesteps import_file's sys.modules caching). Backend selection,
the xctrace argv builder, and availability probing are unit-tested without
invoking any external profiler, since those need full Xcode / Linux tooling.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from mew import profilers
from mew._registry import Entry
from mew.profilers import base, perf, pyspy, xctrace
from mew.profilers.perf import PerfProfiler
from mew.profilers.pyspy import PySpyProfiler
from mew.profilers.xctrace import XctraceProfiler


def _entry(name: str = "bench_x.py::bench_a", *, case_labels: list[str] | None = None) -> Entry:
    """A registry Entry with a source file so each_case yields (the fn is never run:
    the profiler subprocess is mocked)."""
    return Entry(name=name, fn=lambda s: None, file="bench_x.py", case_labels=case_labels)


def _recording_runner() -> tuple[list[list[str]], object]:
    """A subprocess.run stand-in that records argv and reports success."""
    calls: list[list[str]] = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0)

    return calls, run


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


# --- xctrace argv (recorded from a mocked subprocess) ------------------------
# `_record_command` is inlined into run(); we drive run() with subprocess.run
# mocked and assert on the argv it would have launched — no real xctrace needed.


def test_xctrace_combined_run_appends_after_first_case(tmp_path, monkeypatch):
    calls, runner = _recording_runner()
    monkeypatch.setattr(xctrace.subprocess, "run", runner)

    # One family, two cases → two `xctrace record` invocations into one bundle.
    XctraceProfiler().run(
        [_entry("bench.py::f", case_labels=["n=1", "n=2"])],
        output_dir=tmp_path,
        iterations=1000,
    )
    first, later = calls
    assert Path(first[0]).name == "xctrace" and first[1] == "record"
    assert "--append-run" not in first  # first case starts the bundle
    assert "--append-run" in later  # second case appends a run
    # The worker invocation is wired through after `--launch --`.
    tail = first[first.index("--") + 1 :]
    assert tail[:3] == [sys.executable, "-m", "mew._subprocess_worker"]
    assert "bench.py::f" in tail


def test_xctrace_run_passes_time_limit(tmp_path, monkeypatch):
    calls, runner = _recording_runner()
    monkeypatch.setattr(xctrace.subprocess, "run", runner)

    XctraceProfiler().run([_entry()], output_dir=tmp_path, iterations=1000, time_limit="10s")
    (cmd,) = calls
    assert cmd[cmd.index("--time-limit") + 1] == "10s"


_EXPORT_XML = (
    b'<trace-query-result><node><row><backtrace id="b">'
    b'<frame id="f2" name="work" addr="0x2"/><frame id="f1" name="main" addr="0x1"/>'
    b"</backtrace></row></node></trace-query-result>"
)


def _speedscope_runner():
    """A subprocess.run stand-in: `record` is a no-op; `export` writes fixture XML."""

    def run(cmd, **kwargs):
        if "export" in cmd:
            kwargs["stdout"].write(_EXPORT_XML.decode())
        return subprocess.CompletedProcess(cmd, 0)

    return run


def test_xctrace_speedscope_format_combines_cases_into_one_document(tmp_path, monkeypatch):
    monkeypatch.setattr(xctrace.subprocess, "run", _speedscope_runner())

    artifacts = XctraceProfiler().run(
        [_entry("bench.py::f", case_labels=["n=1", "n=2"])],
        output_dir=tmp_path,
        iterations=1000,
        format="speedscope",
    )
    # Both cases resolve to one combined JSON document (the dropdown).
    paths = set(artifacts.values())
    assert len(artifacts) == 2 and len(paths) == 1
    (doc_path,) = paths
    assert doc_path.name == "mew.speedscope.json"
    doc = json.loads(doc_path.read_text())
    assert len(doc["profiles"]) == 2  # one profile per case
    assert doc["profiles"][0]["samples"]  # folded from the exported XML


def test_xctrace_speedscope_separate_writes_one_file_per_case(tmp_path, monkeypatch):
    monkeypatch.setattr(xctrace.subprocess, "run", _speedscope_runner())

    artifacts = XctraceProfiler().run(
        [_entry("bench.py::f", case_labels=["n=1", "n=2"])],
        output_dir=tmp_path,
        iterations=1000,
        format="speedscope",
        separate=True,
    )
    # One single-profile JSON per case.
    assert len(set(artifacts.values())) == 2
    for path in artifacts.values():
        assert path.suffix == ".json" and path.name != "mew.speedscope.json"
        assert len(json.loads(path.read_text())["profiles"]) == 1


@pytest.mark.parametrize("fmt", ["auto", "xctrace"])
def test_xctrace_native_formats_keep_the_trace_bundle(fmt, tmp_path, monkeypatch):
    # `auto` and its tool-named alias `xctrace` both record natively — no export,
    # the artifact is the `.trace` bundle.
    calls, runner = _recording_runner()
    monkeypatch.setattr(xctrace.subprocess, "run", runner)

    artifacts = XctraceProfiler().run(
        [_entry("bench.py::f")], output_dir=tmp_path, iterations=1000, format=fmt
    )
    (path,) = artifacts.values()
    assert path.suffix == ".trace"
    assert not any("export" in cmd for cmd in calls)  # native: recorded, never exported


def test_profile_rejects_format_unsupported_by_backend(monkeypatch):
    from mew import cli, profilers

    class _FakeBackend:
        name = "perf"
        FORMATS = ("auto",)  # perf can't produce the xctrace-native format

    monkeypatch.setattr(profilers, "select", lambda _p: _FakeBackend())
    # Validated before discovery, so it exits without touching the (empty) registry.
    with pytest.raises(SystemExit) as exc:
        cli.profile([], profiler="perf", format="xctrace")
    assert exc.value.code == 2


def test_xctrace_open_routes_collapsed_to_speedscope(tmp_path, monkeypatch):
    opened: list[Path] = []
    monkeypatch.setattr(xctrace, "open_speedscope_artifact", lambda p: opened.append(p))
    instruments, runner = _recording_runner()
    monkeypatch.setattr(xctrace.subprocess, "run", runner)

    backend = XctraceProfiler()
    backend.open_artifact(tmp_path / "mew.speedscope.json")
    backend.open_artifact(tmp_path / "x.trace")
    assert opened == [tmp_path / "mew.speedscope.json"]  # JSON → speedscope
    assert instruments and instruments[0][:3] == [
        "open",
        "-a",
        "Instruments",
    ]  # bundle → Instruments


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


# --- empty-artifact guards (driven through run() with a mocked subprocess) ---
# `py-spy record` / `perf record` exit 0 even when the launched worker died, so a
# failed bench would otherwise leave an empty artifact that reads as success. The
# guard is inlined in run(); we mock the profiler subprocess to fabricate the
# artifact (empty vs real) and assert run() rejects the empty one.


def _pyspy_runner(doc: dict) -> object:
    """subprocess.run stand-in writing `doc` as the speedscope `--output` file."""

    def run(cmd, **kwargs):
        dest = Path(cmd[cmd.index("--output") + 1])
        dest.write_text(json.dumps(doc))
        return subprocess.CompletedProcess(cmd, 0)

    return run


def test_pyspy_run_rejects_empty_profile(tmp_path, monkeypatch):
    monkeypatch.setattr(
        pyspy.subprocess, "run", _pyspy_runner({"shared": {"frames": []}, "profiles": []})
    )
    with pytest.raises(SystemExit, match="no samples"):
        PySpyProfiler().run([_entry()], output_dir=tmp_path, iterations=1)


def test_pyspy_run_accepts_real_profile(tmp_path, monkeypatch):
    doc = {"shared": {"frames": [{"name": "f"}]}, "profiles": [{"samples": [[0]]}]}
    monkeypatch.setattr(pyspy.subprocess, "run", _pyspy_runner(doc))
    artifacts = PySpyProfiler().run([_entry()], output_dir=tmp_path, iterations=1)
    assert len(artifacts) == 1


def _perf_runner(script: str) -> object:
    """subprocess.run stand-in: `perf script` writes `script` to its stdout file."""

    def run(cmd, **kwargs):
        # The `perf script` call passes stdout=<open dest>; `perf record` does not.
        out = kwargs.get("stdout")
        if out is not None:
            out.write(script)
        return subprocess.CompletedProcess(cmd, 0)

    return run


def test_perf_run_rejects_empty_script(tmp_path, monkeypatch):
    monkeypatch.setattr(perf.subprocess, "run", _perf_runner("   \n"))
    with pytest.raises(SystemExit, match="no samples"):
        PerfProfiler().run([_entry()], output_dir=tmp_path, iterations=1)


def test_perf_run_accepts_nonempty_script(tmp_path, monkeypatch):
    script = "python 1234 [000] 0.1: cycles:\n\t  ffff _start\n"
    monkeypatch.setattr(perf.subprocess, "run", _perf_runner(script))
    artifacts = PerfProfiler().run([_entry()], output_dir=tmp_path, iterations=1)
    assert len(artifacts) == 1

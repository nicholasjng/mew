"""In-process profiling: the Google Benchmark memory/profiler managers."""

from __future__ import annotations

import json
import sys
from typing import Any, cast

import pytest

import mew
from mew.reporter import JSONReporter


class FakeMemoryManager:
    """Memory manager returning fixed figures, so the stamp path is testable
    without memray installed."""

    def __init__(self, **figures: int) -> None:
        self._figures = figures or {
            "peak_bytes": 1024,
            "total_bytes": 2048,
            "total_allocations": 5,
        }
        self.starts = 0
        self.stops = 0

    def start(self) -> None:
        self.starts += 1

    def stop(self) -> mew.MemoryMetrics:
        self.stops += 1
        return cast(mew.MemoryMetrics, self._figures.copy())


class FakeProfilerManager:
    """Profiler manager returning a fixed summary, standing in for pyinstrument."""

    _DEFAULT = {  # ruff: ignore[RUF012]
        "profiler": "pyinstrument",
        "wall_time": 0.5,
        "sample_count": 500,
        "top_function": "foo (bar.py:1)",
        "top_function_total_self_time": 0.3,
    }

    def __init__(self, result: dict | None = _DEFAULT) -> None:
        # Default sentinel, not `None`: `result=None` means "report nothing".
        self._result = result
        self.starts = 0
        self.stops = 0
        self.pauses = 0
        self.resumes = 0

    def after_setup_start(self) -> None:
        self.starts += 1

    def before_teardown_stop(self) -> None:
        self.stops += 1

    def pause(self) -> None:
        self.pauses += 1

    def resume(self) -> None:
        self.resumes += 1

    def get_result(self) -> dict | None:
        return self._result


def test_registering_a_manager_replaces_the_previous_one(tmp_path):
    """Matches upstream GB, where registration is a plain replace."""
    from mew import _core

    @mew.benchmark(iterations=2)
    def bench_x(state):
        for _ in state:
            pass

    first_mem, second_mem = FakeMemoryManager(), FakeMemoryManager()
    first_prof, second_prof = FakeProfilerManager(), FakeProfilerManager()
    _core.register_memory_manager(first_mem)
    _core.register_profiler_manager(first_prof)
    try:
        _core.register_memory_manager(second_mem)
        _core.register_profiler_manager(second_prof)
        mew.run(reporter=JSONReporter(output=tmp_path / "o.json"))
    finally:
        _core.unregister_memory_manager()
        _core.unregister_profiler_manager()
    assert first_mem.starts == first_prof.starts == 0
    assert second_mem.starts == second_mem.stops == 1
    assert second_prof.starts == second_prof.stops == 1


# --- manager registration and the Run stamp ----------------------------------


def test_memory_manager_is_driven_and_stamped_onto_rows(tmp_path):
    @mew.benchmark
    def bench_mem(state):
        for _ in state:
            pass

    mgr = FakeMemoryManager()
    out = tmp_path / "out.json"
    mew.run(min_time="1x", reporter=JSONReporter(output=out), memory_manager=mgr)

    assert mgr.starts == mgr.stops == 1
    bench = json.loads(out.read_text())["benchmarks"][0]
    assert bench["memory"]["peak_bytes"] == 1024
    assert bench["memory"]["total_bytes"] == 2048
    assert bench["memory"]["total_allocations"] == 5
    # memory_iterations is GB's own min(16, iters), and the per-iteration rate
    # is derived from it rather than supplied by the manager.
    assert bench["memory"]["iterations"] == 1
    assert bench["memory"]["allocations_per_iteration"] == pytest.approx(
        5 / bench["memory"]["iterations"]
    )


def test_profiler_manager_is_driven_and_stamped_onto_rows(tmp_path):
    @mew.benchmark
    def bench_cpu(state):
        for _ in state:
            pass

    mgr = FakeProfilerManager()
    out = tmp_path / "out.json"
    mew.run(min_time="1x", reporter=JSONReporter(output=out), profiler_manager=mgr)

    assert mgr.starts == mgr.stops == 1
    bench = json.loads(out.read_text())["benchmarks"][0]
    assert bench["cpu_profile"] == {
        "profiler": "pyinstrument",
        "wall_time": 0.5,
        "sample_count": 500,
        "top_function": "foo (bar.py:1)",
        "top_function_total_self_time": 0.3,
    }


def test_profiler_manager_returning_none_leaves_row_unannotated(tmp_path):
    @mew.benchmark
    def bench_nosamples(state):
        for _ in state:
            pass

    out = tmp_path / "out.json"
    mew.run(
        min_time="1x",
        reporter=JSONReporter(output=out),
        profiler_manager=FakeProfilerManager(result=None),
    )
    assert "cpu_profile" not in json.loads(out.read_text())["benchmarks"][0]


def test_profiler_manager_get_result_is_optional(tmp_path):
    class MinimalProfilerManager:
        def after_setup_start(self) -> None:
            pass

        def before_teardown_stop(self) -> None:
            pass

    @mew.benchmark
    def bench_minimal(state):
        for _ in state:
            pass

    out = tmp_path / "out.json"
    mew.run(
        min_time="1x",
        reporter=JSONReporter(output=out),
        profiler_manager=MinimalProfilerManager(),
    )
    assert "cpu_profile" not in json.loads(out.read_text())["benchmarks"][0]


@pytest.mark.parametrize("reuse_scope", [False, True])
def test_nested_pauses_toggle_the_profiler_only_at_the_outer_scope(tmp_path, reuse_scope):
    mgr = FakeProfilerManager()

    @mew.benchmark(iterations=2)
    def bench_nested(state):
        for _ in state:
            outer = state.pause()
            with outer:
                resumes = mgr.resumes
                with outer if reuse_scope else state.pause():
                    pass
                assert mgr.resumes == resumes

    mew.run(reporter=JSONReporter(output=tmp_path / "o.json"), profiler_manager=mgr)
    assert mgr.starts == mgr.stops == 1
    assert mgr.pauses == mgr.resumes == 2


def test_managers_do_not_leak_into_a_later_run(tmp_path):
    @mew.benchmark
    def bench_leak(state):
        for _ in state:
            pass

    mgr = FakeMemoryManager()
    mew.run(min_time="1x", reporter=JSONReporter(output=tmp_path / "a.json"), memory_manager=mgr)
    after_first = mgr.starts

    out = tmp_path / "b.json"
    mew.run(min_time="1x", reporter=JSONReporter(output=out))
    # GB's manager registration is process-global; the scope must unregister it.
    assert mgr.starts == after_first
    bench = json.loads(out.read_text())["benchmarks"][0]
    assert "memory" not in bench
    assert "cpu_profile" not in bench


def test_memory_pass_completion_exception_propagates_out_of_run(tmp_path):
    class Exploding(FakeMemoryManager):
        def on_pass_complete(self, completed):
            raise RuntimeError("capture publication failed")

    @mew.benchmark(iterations=1)
    def bench_complete(state):
        for _ in state:
            pass

    with pytest.raises(RuntimeError, match="capture publication failed"):
        mew.run(reporter=JSONReporter(output=tmp_path / "o.json"), memory_manager=Exploding())


def test_memory_pass_is_not_accepted_when_stop_raises(tmp_path):
    class Exploding(FakeMemoryManager):
        completed: list[bool]

        def __init__(self):
            super().__init__()
            self.completed = []

        def stop(self):
            raise RuntimeError("capture unreadable")

        def on_pass_complete(self, completed):
            self.completed.append(completed)

    @mew.benchmark(iterations=1)
    def bench_complete(state):
        for _ in state:
            pass

    manager = Exploding()
    with pytest.raises(RuntimeError, match="capture unreadable"):
        mew.run(reporter=JSONReporter(output=tmp_path / "o.json"), memory_manager=manager)
    assert manager.completed == [False]


@pytest.mark.parametrize("kind", ["memory", "profiler"])
def test_malformed_manager_result_propagates_out_of_run(tmp_path, kind):
    """A wrong-shaped manager result fails the run instead of being dropped silently."""

    @mew.benchmark
    def bench_bad_result(state):
        for _ in state:
            pass

    kwargs: dict[str, Any]
    if kind == "memory":

        class BadMemory(FakeMemoryManager):
            def stop(self):
                return {"peak_bytes": "not an integer"}

        kwargs = {"memory_manager": BadMemory()}
    else:

        class BadProfiler(FakeProfilerManager):
            def get_result(self):
                return {"sample_count": object()}

        kwargs = {"profiler_manager": BadProfiler()}

    with pytest.raises(TypeError, match="must return"):
        mew.run(
            min_time="1x",
            reporter=JSONReporter(output=tmp_path / "bad.json"),
            **kwargs,
        )


# --- the real backends -------------------------------------------------------


def test_memray_manager_reports_loop_allocations(tmp_path):
    """Scoped to the timing loop: a large fixture allocated before the loop must
    not appear in the tracked figures."""
    pytest.importorskip("memray")
    from contextlib import ExitStack

    from mew import memory as _memory

    setup_bytes = 50_000_000

    @mew.benchmark
    def bench_setup_heavy(state):
        fixture = bytearray(setup_bytes)  # setup: outside the capture window
        data = None
        for _ in state:
            data = bytearray(10_000)
        del fixture, data

    out = tmp_path / "out.json"
    with ExitStack() as stack:
        mew.run(
            min_time="20x",
            reporter=JSONReporter(output=out),
            memory_manager=_memory.manager(stack),
        )
    mem = json.loads(out.read_text())["benchmarks"][0]["memory"]
    assert mem["peak_bytes"] < setup_bytes / 10
    assert "total_bytes" not in mem
    assert mem["allocations_per_iteration"] == pytest.approx(
        mem["total_allocations"] / mem["iterations"]
    )


@pytest.mark.skipif(
    not getattr(sys, "_is_gil_enabled", lambda: True)(),
    reason="pyinstrument's native sampler enables the GIL",
)
def test_pyinstrument_manager_summarizes_the_hot_frame(tmp_path):
    pytest.importorskip("pyinstrument")
    from mew import cpu as _cpu

    def spin() -> int:
        # Self time must land in `spin` itself, so loop in Python rather than
        # deferring to a C builtin like sum().
        total = 0
        for i in range(20_000):
            total += i
        return total

    @mew.benchmark
    def bench_hot(state):
        for _ in state:
            spin()

    out = tmp_path / "out.json"
    mgr = _cpu.PyinstrumentManager(interval=1e-5)
    mew.run(min_time="200x", reporter=JSONReporter(output=out), profiler_manager=mgr)

    cpu = json.loads(out.read_text())["benchmarks"][0].get("cpu_profile")
    assert cpu is not None, "sampler collected nothing; raise the iteration count"
    assert cpu["profiler"] == "pyinstrument"
    assert cpu["sample_count"] > 0
    assert "spin" in cpu["top_function"]
    # Sessions are retained only so --sample-html can render one combined report.
    assert mgr.sessions
    sessions = len(mgr.sessions)
    assert mgr.get_result() is not None
    assert len(mgr.sessions) == sessions


@pytest.mark.parametrize("kind", ["memory", "profiler"])
@pytest.mark.parametrize("exit_at", ["before", "first", "last"])
@pytest.mark.parametrize("exit_kind", ["return", "skip"])
def test_rejected_passes_do_not_publish_captures(tmp_path, kind, exit_at, exit_kind):
    import time

    if kind == "memory":
        pytest.importorskip("memray")
        from mew.memory import MemrayManager

        manager: Any = MemrayManager(tmp_path)
        retained = manager.captures
    else:
        if not getattr(sys, "_is_gil_enabled", lambda: True)():
            pytest.skip("pyinstrument's native sampler enables the GIL")
        pytest.importorskip("pyinstrument")
        from mew.cpu import PyinstrumentManager

        manager = cast("Any", PyinstrumentManager())
        retained = manager.sessions

    calls = 0

    @mew.benchmark(iterations=4, repetitions=3)
    def bench_partial(state):
        nonlocal calls
        calls += 1
        # Manager passes are invocations 2, 4 and 6. Reject the first and last
        # to check that neither an earlier nor a later failure loses a good capture.
        reject = calls in (2, 6)
        if reject and exit_at == "before":
            if exit_kind == "skip":
                state.skip_with_message("incomplete manager pass")
            return
        for iteration, _ in enumerate(state, 1):
            data = bytearray(16_384)
            time.sleep(0.004)  # collect enough CPU samples for a summary
            del data
            if reject and iteration == (4 if exit_at == "last" else 1):
                if exit_kind == "skip":
                    state.skip_with_message("incomplete manager pass")
                return

    out = tmp_path / "out.json"
    kwargs: dict[str, Any] = {f"{kind}_manager": manager}
    mew.run(reporter=JSONReporter(output=out), **kwargs)
    rows = [r for r in json.loads(out.read_text())["benchmarks"] if r["run_type"] == "iteration"]
    key = "memory" if kind == "memory" else "cpu_profile"
    assert len(rows) == 3
    assert all(not r["skipped"] for r in rows)
    assert [key in r for r in rows] == [False, True, False]
    assert len(retained) == 1
    if kind == "memory":
        assert manager._tracker is None
        assert manager._pending_capture is None
        # Repeated notifications cannot publish the same capture twice.
        manager.on_pass_complete(True)
    else:
        assert manager._prof is None
    assert len(retained) == 1


@pytest.mark.parametrize(("memory_iterations", "expected"), [(None, 16), (4, 4), (1000, 50)])
def test_memory_iterations_caps_the_memory_pass(tmp_path, memory_iterations, expected):
    @mew.benchmark(iterations=50)
    def bench_x(state):
        for _ in state:
            pass

    out = tmp_path / "out.json"
    mew.run(
        reporter=JSONReporter(output=out),
        memory_manager=FakeMemoryManager(),
        memory_iterations=memory_iterations,
    )
    mem = json.loads(out.read_text())["benchmarks"][0]["memory"]
    assert mem["iterations"] == expected


@pytest.mark.skipif(
    getattr(sys, "_is_gil_enabled", lambda: True)(),
    reason="threaded mode requires a free-threaded interpreter",
)
def test_manager_passes_run_with_the_configured_thread_count(tmp_path):
    """The profiler pass spawns every worker like the timed run does, and the
    manager hooks fire once per pass rather than once per thread."""
    seen: list[int] = []

    @mew.benchmark(threads=2, iterations=10)
    def bench_x(state):
        seen.append(state.thread_index)
        for _ in state:
            pass

    mgr = FakeProfilerManager()
    mew.run(reporter=JSONReporter(output=tmp_path / "o.json"), profiler_manager=mgr)
    assert mgr.starts == 1 and mgr.stops == 1
    # Timed run + profiler pass: both threads ran in both.
    assert seen.count(0) >= 2 and seen.count(1) >= 2


@pytest.mark.skipif(
    getattr(sys, "_is_gil_enabled", lambda: True)(),
    reason="threaded mode requires a free-threaded interpreter",
)
def test_threaded_memory_pass_counts_every_threads_iterations(tmp_path):
    @mew.benchmark(threads=2, iterations=10)
    def bench_x(state):
        for _ in state:
            pass

    out = tmp_path / "out.json"
    mew.run(
        reporter=JSONReporter(output=out),
        memory_manager=FakeMemoryManager(total_allocations=8),
        memory_iterations=4,
    )
    mem = json.loads(out.read_text())["benchmarks"][0]["memory"]
    # 4 iterations on each of 2 threads, like the timed row's iteration count.
    assert mem["iterations"] == 8
    assert mem["allocations_per_iteration"] == 1.0


def test_memory_pass_counts_batch_overshoot(tmp_path):
    @mew.benchmark(iterations=20)
    def bench_batch(state):
        for _ in state.batches(10):
            pass

    out = tmp_path / "out.json"
    mew.run(
        reporter=JSONReporter(output=out),
        memory_manager=FakeMemoryManager(total_allocations=10),
        memory_iterations=4,
    )
    mem = json.loads(out.read_text())["benchmarks"][0]["memory"]
    assert mem["iterations"] == 10
    assert mem["allocations_per_iteration"] == 1.0


@pytest.mark.parametrize("kind", ["memory", "profiler"])
@pytest.mark.parametrize("exit_at", ["before", "first", "last"])
def test_partial_manager_pass_is_not_reported(tmp_path, kind, exit_at):
    calls = 0

    @mew.benchmark(iterations=4)
    def bench_partial(state):
        nonlocal calls
        calls += 1
        if calls == 2 and exit_at == "before":
            return
        for iteration, _ in enumerate(state, 1):
            if calls == 2 and iteration == (4 if exit_at == "last" else 1):
                return

    manager = FakeMemoryManager() if kind == "memory" else FakeProfilerManager()
    kwargs: dict[str, Any] = {f"{kind}_manager": manager}
    out = tmp_path / "out.json"
    mew.run(reporter=JSONReporter(output=out), **kwargs)
    row = json.loads(out.read_text())["benchmarks"][0]
    assert not row["skipped"]
    assert ("memory" if kind == "memory" else "cpu_profile") not in row
    assert manager.starts == manager.stops == (0 if exit_at == "before" else 1)


@pytest.mark.filterwarnings("ignore::pytest.PytestUnraisableExceptionWarning")
def test_manager_is_stopped_when_the_body_leaves_the_loop_early(tmp_path):
    @mew.benchmark(iterations=4)
    def bench_raises(state):
        for _ in state:
            raise ValueError("boom")

    @mew.benchmark(iterations=4)
    def bench_ok(state):
        for _ in state:
            pass

    mem, prof = FakeMemoryManager(), FakeProfilerManager()
    out = tmp_path / "out.json"
    mew.run(reporter=JSONReporter(output=out), memory_manager=mem, profiler_manager=prof)
    assert (mem.starts, mem.stops) == (2, 2)
    assert (prof.starts, prof.stops) == (2, 2)
    raises, ok = json.loads(out.read_text())["benchmarks"]
    assert "memory" not in raises and "cpu_profile" not in raises
    assert "memory" in ok and "cpu_profile" in ok


def test_pyinstrument_is_rejected_before_import_on_free_threaded(monkeypatch):
    from mew import cpu as _cpu

    monkeypatch.setattr(_cpu, "_gil_enabled", lambda: False)
    with pytest.raises(SystemExit, match="does not support free-threaded Python"):
        _cpu.require_pyinstrument()


def test_hottest_frame_excludes_pyinstrument_internals(tmp_path):
    from mew.cpu import _hottest_frame

    class Frame:
        is_synthetic = False
        line_no = 1

        def __init__(self, function, file_path, self_time, children=()):
            self.function = function
            self.file_path = file_path
            self.total_self_time = self_time
            self.children = list(children)

    user = Frame("work", str(tmp_path / "project" / "bench.py"), 0.1)
    sampler = Frame(
        "_start_sampling",
        str(tmp_path / "venv" / "pyinstrument" / "stack_sampler.py"),
        1.0,
        [user],
    )

    assert _hottest_frame(cast("Any", sampler)) == ("work (bench.py:1)", 0.1)


@pytest.mark.parametrize("interval", [0, -1, float("nan"), float("inf")])
def test_pyinstrument_manager_rejects_invalid_interval(interval):
    from mew.cpu import PyinstrumentManager

    with pytest.raises(ValueError, match="interval"):
        PyinstrumentManager(interval=interval)


def test_flamegraph_is_rooted_at_the_benchmark_and_loop_scoped(tmp_path):
    """The graph names each benchmark and covers the same region as the table.

    Re-rooting puts back the frame memray drops from a loop-scoped capture,
    including for body-level allocations, which otherwise carry no stack."""
    pytest.importorskip("memray")
    from contextlib import ExitStack

    from mew import memory as _memory

    def nested_alloc_helper() -> bytearray:
        return bytearray(600_000)  # allocated one frame below the body

    @mew.benchmark
    def bench_rooted(state):
        fixture = bytearray(30_000_000)  # setup: excluded from the loop scope
        for _ in state:
            nested = nested_alloc_helper()
            inline = bytearray(400_000)  # allocated directly in the body frame
            del inline, nested
        del fixture

    out = tmp_path / "flame.html"
    with ExitStack() as stack:
        manager = _memory.manager(stack)
        mew.run(min_time="20x", reporter=None, memory_manager=manager)
        assert manager.captures, "no capture recorded"
        # Each capture is tagged with the user's benchmark frame, not a mew internal.
        assert all(root[0] == "bench_rooted" for _, root in manager.captures)
        _memory.write_flamegraph(manager, out)

    html = out.read_text()
    assert "bench_rooted" in html
    # A multi-frame stack renders through memray's real reporter, which pins the
    # attributes `_RootedRecord` must duck-type from `AllocationRecord`.
    assert "nested_alloc_helper" in html


def test_flamegraph_header_covers_every_capture(tmp_path, monkeypatch):
    """The combined report's header totals span all captures, not the first."""
    pytest.importorskip("memray")
    from contextlib import ExitStack

    import memray
    from memray.reporters.flamegraph import FlameGraphReporter

    from mew import memory as _memory

    @mew.benchmark(iterations=2)
    def bench_small(state):
        for _ in state:
            buf = bytearray(1_000)
            del buf

    @mew.benchmark(iterations=2)
    def bench_large(state):
        for _ in state:
            buf = bytearray(5_000_000)
            del buf

    rendered = {}
    monkeypatch.setattr(
        FlameGraphReporter, "render", lambda self, f, *, metadata, **kw: rendered.update(m=metadata)
    )
    with ExitStack() as stack:
        manager = _memory.manager(stack)
        mew.run(reporter=None, memory_manager=manager)
        metas = []
        for capture, _ in manager.captures:
            with memray.FileReader(capture) as reader:
                metas.append(reader.metadata)
        _memory.write_flamegraph(manager, tmp_path / "flame.html")

    assert len(metas) == 2
    assert rendered["m"].total_allocations == sum(m.total_allocations for m in metas)
    assert rendered["m"].peak_memory == max(m.peak_memory for m in metas)


def test_write_flamegraph_warns_when_nothing_was_captured(tmp_path, capsys):
    pytest.importorskip("memray")
    from contextlib import ExitStack

    from mew import memory as _memory

    out = tmp_path / "flame.html"
    with ExitStack() as stack:
        _memory.write_flamegraph(_memory.manager(stack), out)
    assert not out.exists()
    assert "no memory captures recorded" in capsys.readouterr().err


def test_pause_only_reaches_the_profiler_during_its_own_pass(tmp_path):
    """GB drives the timed run with no profiler manager, so `state.pause()` there
    must not call one: it would suspend nothing, and in a threaded run several
    worker threads would race on the manager's depth counter."""
    calls: list[str] = []

    class Probe:
        def __init__(self) -> None:
            self.sampling = False

        def after_setup_start(self) -> None:
            self.sampling = True

        def before_teardown_stop(self) -> None:
            self.sampling = False

        def get_result(self) -> None:
            return None

        def pause(self) -> None:
            calls.append("sampling" if self.sampling else "idle")

        def resume(self) -> None:
            pass

    @mew.benchmark(name="paused")
    def bench_paused(state):
        for _ in state:
            with state.pause():
                pass

    mew.run(
        min_time="20x", reporter=JSONReporter(output=tmp_path / "o.json"), profiler_manager=Probe()
    )
    assert calls, "the profiler pass must still see its pauses"
    assert set(calls) == {"sampling"}


@pytest.mark.skipif(
    not getattr(sys, "_is_gil_enabled", lambda: True)(),
    reason="pyinstrument's native sampler enables the GIL",
)
def test_pyinstrument_tolerates_a_pause_scope_open_at_loop_end(tmp_path):
    pytest.importorskip("pyinstrument")
    from mew.cpu import PyinstrumentManager

    @mew.benchmark(iterations=2)
    def bench_open(state):
        for _ in state:
            state.pause().__enter__()  # never exited

    @mew.benchmark(iterations=2)
    def bench_next(state):
        for _ in state:
            with state.pause():
                pass

    out = tmp_path / "o.json"
    mgr = PyinstrumentManager()
    mew.run(reporter=JSONReporter(output=out), profiler_manager=mgr)
    rows = json.loads(out.read_text())["benchmarks"]
    assert [r["skipped"] for r in rows] == [False, False]
    assert len(mgr.sessions) == 2


@pytest.mark.skipif(
    getattr(sys, "_is_gil_enabled", lambda: True)(),
    reason="threaded mode requires a free-threaded interpreter",
)
def test_only_the_profiling_thread_pauses_the_profiler(tmp_path):
    """The profiler pass hands the manager to thread 0 only; other workers'
    pauses must not toggle it from under that thread."""
    import threading

    callers: set[int] = set()

    class Recording(FakeProfilerManager):
        def pause(self) -> None:
            callers.add(threading.get_ident())
            super().pause()

    mgr = Recording()

    @mew.benchmark(threads=2, iterations=3)
    def bench_x(state):
        for _ in state:
            with state.pause():
                pass

    mew.run(reporter=JSONReporter(output=tmp_path / "o.json"), profiler_manager=mgr)
    assert len(callers) == 1
    assert mgr.pauses == mgr.resumes == 3

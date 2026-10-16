"""In-process profiling: the Google Benchmark memory/profiler managers."""

from __future__ import annotations

import inspect
import json
import math
import random
import sys
import types
import typing
from typing import Any, cast

import pytest
from _helpers import Capture

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
        "top_function": {"function": "foo", "file": "bar.py", "line": None, "self_time": 0.3},
        "top_instructions": [],
        "unspecialized_share": None,
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
    assert bench["cpu_profile"] == FakeProfilerManager._DEFAULT


def test_profiler_manager_result_keeps_its_json_types(tmp_path):
    """The summary crosses Google Benchmark as JSON text, so every value comes
    back as json.loads(json.dumps(value)) would return it."""
    result = {
        "profiler": "fake · ünïcode",
        "sample_count": 2**70,
        "share": 0.1,
        "flag": True,
        "missing": None,
        "nan": float("nan"),
        "top": [{"name": "a", "line": 7, "share": 0.5, "flag": False, "none": None}],
        "record": {"k": ["v", 1], "nested": {"deep": [[1, 2], []]}},
        "pair": (1, "a"),
    }

    @mew.benchmark
    def bench_structured(state):
        for _ in state:
            pass

    rep = Capture()
    mew.run(min_time="1x", reporter=rep, profiler_manager=FakeProfilerManager(result=result))
    cpu = rep.runs[0]["cpu_profile"]
    assert math.isnan(cpu.pop("nan"))
    assert cpu == {**{k: v for k, v in result.items() if k != "nan"}, "pair": [1, "a"]}
    assert type(cpu["sample_count"]) is int
    assert cpu["flag"] is True
    assert cpu["missing"] is None


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
    errors: list[str] = []
    resumed_early: list[bool] = []

    @mew.benchmark(iterations=2)
    def bench_nested(state):
        for _ in state:
            outer = state.pause()
            with outer:
                resumes = mgr.resumes
                try:
                    with outer if reuse_scope else state.pause():
                        pass
                except RuntimeError as exc:
                    errors.append(str(exc))
                resumed_early.append(mgr.resumes != resumes)

    mew.run(reporter=JSONReporter(output=tmp_path / "o.json"), profiler_manager=mgr)
    assert mgr.starts == mgr.stops == 1
    # From the profiler pass alone: a timed-run pause must not reach the manager,
    # where it would suspend nothing and worker threads would race on its state.
    assert mgr.pauses == mgr.resumes == 2
    assert resumed_early and not any(resumed_early)
    # A fresh inner scope does nothing; re-entering the outer one raises.
    assert len(errors) == (len(resumed_early) if reuse_scope else 0)


def test_managers_do_not_leak_into_a_later_run(tmp_path):
    @mew.benchmark
    def bench_leak(state):
        for _ in state:
            pass

    mem, prof = FakeMemoryManager(), FakeProfilerManager()
    mew.run(
        min_time="1x",
        reporter=JSONReporter(output=tmp_path / "a.json"),
        memory_manager=mem,
        profiler_manager=prof,
    )
    after_first = mem.starts, prof.starts

    out = tmp_path / "b.json"
    mew.run(min_time="1x", reporter=JSONReporter(output=out))
    # GB's manager registration is process-global; the scope must unregister both.
    assert (mem.starts, prof.starts) == after_first
    bench = json.loads(out.read_text())["benchmarks"][0]
    assert "memory" not in bench
    assert "cpu_profile" not in bench


def test_memory_manager_stop_exception_propagates_out_of_run(tmp_path):
    class Exploding(FakeMemoryManager):
        def stop(self):
            raise RuntimeError("capture unreadable")

    @mew.benchmark(iterations=1)
    def bench_complete(state):
        for _ in state:
            pass

    with pytest.raises(RuntimeError, match="capture unreadable"):
        mew.run(reporter=JSONReporter(output=tmp_path / "o.json"), memory_manager=Exploding())


def test_memory_manager_extra_keys_are_ignored(tmp_path):
    class Annotated(FakeMemoryManager):
        def stop(self):
            return {"peak_bytes": 1024, "backend": "custom"}

    @mew.benchmark(iterations=1)
    def bench_annotated(state):
        for _ in state:
            pass

    out = tmp_path / "o.json"
    mew.run(reporter=JSONReporter(output=out), memory_manager=Annotated())
    mem = json.loads(out.read_text())["benchmarks"][0]["memory"]
    assert mem["peak_bytes"] == 1024
    assert "backend" not in mem


@pytest.mark.parametrize(
    ("kind", "result"),
    [
        ("memory", {"peak_bytes": "not an integer"}),
        ("memory", [("peak_bytes", 1)]),
        ("profiler", {"sample_count": object()}),
        ("profiler", {"top": [object()]}),
        ("profiler", {1: 2.0}),
        ("profiler", [("sample_count", 1)]),
    ],
)
def test_malformed_manager_result_propagates_out_of_run(tmp_path, kind, result):
    """A wrong-shaped manager result fails the run instead of being dropped silently."""

    @mew.benchmark
    def bench_bad_result(state):
        for _ in state:
            pass

    kwargs: dict[str, Any]
    if kind == "memory":

        class BadMemory(FakeMemoryManager):
            def stop(self):
                return result

        kwargs = {"memory_manager": BadMemory()}
    else:

        class BadProfiler(FakeProfilerManager):
            def get_result(self):
                return result

        kwargs = {"profiler_manager": BadProfiler()}

    with pytest.raises(TypeError, match="must return"):
        mew.run(
            min_time="1x",
            reporter=JSONReporter(output=tmp_path / "bad.json"),
            **kwargs,
        )


# --- the real backends -------------------------------------------------------

# For tests that need samples from sub-millisecond bodies: a sampler starved by a
# loaded machine can collect nothing in a shorter pass.
_SAMPLED_ITERATIONS = "200x"

needs_tachyon = pytest.mark.skipif(
    sys.version_info < (3, 15), reason="Tachyon ships with Python 3.15+"
)

# A skip mark, so pytest skips before the test body imports pyinstrument.
_SAMPLERS = [
    pytest.param(
        "pyinstrument",
        marks=pytest.mark.skipif(
            not mew.gil_enabled(),
            reason="pyinstrument's native sampler enables the GIL",
        ),
    ),
    pytest.param("tachyon", marks=needs_tachyon),
]


def _sampled_manager(sampler: str) -> Any:
    """``sampler``'s manager; pyinstrument's samples finer than its default."""
    from mew import cpu as _cpu

    if sampler == "tachyon":
        return _cpu.TachyonManager()
    pytest.importorskip("pyinstrument")
    return _cpu.PyinstrumentManager(interval=1e-5)


def _spin() -> int:
    # Self time must land in `_spin` itself, so loop in Python rather than
    # deferring to a C builtin like sum().
    total = 0
    for i in range(20_000):
        total += i
    return total


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
    mew.gil_enabled(),
    reason="threaded mode requires a free-threaded interpreter",
)
def test_manager_passes_run_a_single_thread(tmp_path):
    """As in Google Benchmark, the profiler pass runs thread 0 alone, and
    `state.threads` says so: a barrier sized by it must not wait for absent
    workers."""
    import threading

    seen: list[tuple[int, int]] = []
    # One barrier per reported thread count, as a body sized by it would build.
    barriers = {n: threading.Barrier(n) for n in (1, 2)}

    @mew.benchmark(threads=2, iterations=10)
    def bench_x(state):
        seen.append((state.thread_index, state.threads))
        barriers[state.threads].wait(timeout=10)
        for _ in state:
            pass

    mgr = FakeProfilerManager()
    mew.run(reporter=JSONReporter(output=tmp_path / "o.json"), profiler_manager=mgr)
    assert mgr.starts == 1 and mgr.stops == 1
    # Timed run: both threads see 2. Profiler pass: thread 0 alone sees 1.
    assert sorted(seen[:2]) == [(0, 2), (1, 2)]
    assert seen[2:] == [(0, 1)]


@pytest.mark.skipif(
    mew.gil_enabled(),
    reason="threaded mode requires a free-threaded interpreter",
)
def test_threaded_memory_pass_runs_one_thread(tmp_path):
    @mew.benchmark(threads=2, iterations=10)
    def bench_x(state):
        for _ in state:
            pass

    out = tmp_path / "out.json"
    mew.run(
        reporter=JSONReporter(output=out),
        memory_manager=FakeMemoryManager(total_allocations=4),
        memory_iterations=4,
    )
    mem = json.loads(out.read_text())["benchmarks"][0]["memory"]
    # 4 iterations on the pass's single thread.
    assert mem["iterations"] == 4
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
def test_interrupted_pass_still_stops_the_manager(tmp_path, kind):
    calls = 0

    @mew.benchmark(iterations=4)
    def bench_interrupted(state):
        nonlocal calls
        calls += 1
        for _ in state:
            if calls == 2:  # the manager pass, after the timed run
                raise KeyboardInterrupt

    manager = FakeMemoryManager() if kind == "memory" else FakeProfilerManager()
    kwargs: dict[str, Any] = {f"{kind}_manager": manager}
    with pytest.raises(KeyboardInterrupt):
        mew.run(reporter=JSONReporter(output=tmp_path / "out.json"), **kwargs)
    assert manager.starts == manager.stops == 1


def test_failed_timed_runs_skip_the_manager_passes(tmp_path):
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
    # Only bench_ok gets memory and profiler passes.
    assert (mem.starts, mem.stops) == (1, 1)
    assert (prof.starts, prof.stops) == (1, 1)
    raises, ok = json.loads(out.read_text())["benchmarks"]
    assert "memory" not in raises and "cpu_profile" not in raises
    assert "memory" in ok and "cpu_profile" in ok


def test_pyinstrument_is_rejected_before_import_on_free_threaded(monkeypatch):
    from mew import cpu as _cpu

    monkeypatch.setattr(_cpu, "gil_enabled", lambda: False)
    with pytest.raises(SystemExit, match="does not support free-threaded Python"):
        _cpu.PyinstrumentManager()


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

    assert _hottest_frame(cast("Any", sampler)) == {
        "function": "work",
        "file": "bench.py",
        "line": None,
        "self_time": 0.1,
    }
    # Nothing but pyinstrument's own frames: no hottest frame to name.
    assert _hottest_frame(cast("Any", Frame("_start_sampling", sampler.file_path, 1.0))) is None


@pytest.mark.parametrize("manager", ["PyinstrumentManager", "TachyonManager"])
@pytest.mark.parametrize("interval", [0, -1, float("nan"), float("inf")])
def test_cpu_managers_reject_invalid_interval(manager, interval):
    from mew import cpu as _cpu

    with pytest.raises(ValueError, match="interval"):
        getattr(_cpu, manager)(interval=interval)


def test_tachyon_manager_rejects_top_k_below_one():
    from mew.cpu import TachyonManager

    with pytest.raises(ValueError, match="top_k"):
        TachyonManager(top_k=0)


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


@pytest.mark.parametrize("sampler", _SAMPLERS)
def test_write_html_warns_when_nothing_was_sampled(tmp_path, capsys, sampler):
    from mew import cpu as _cpu

    out = tmp_path / "cpu.html"
    _cpu.write_html(_sampled_manager(sampler), out)
    assert not out.exists()
    assert "no CPU samples collected" in capsys.readouterr().err


def test_pause_held_across_the_loop_end_is_not_resumed_after_the_pass(tmp_path):
    """The profiler stops while still paused; the scope's exit after the loop
    must not resume a manager whose pass is over."""
    from contextlib import ExitStack

    mgr = FakeProfilerManager()

    @mew.benchmark(iterations=3)
    def bench_held(state):
        with ExitStack() as stack:
            for _ in state:
                stack.enter_context(state.pause())

    mew.run(reporter=JSONReporter(output=tmp_path / "o.json"), profiler_manager=mgr)
    assert mgr.starts == mgr.stops == 1
    assert (mgr.pauses, mgr.resumes) == (1, 0)


@pytest.mark.parametrize("sampler", _SAMPLERS)
def test_sampler_handles_a_pause_held_over_the_loop_end(tmp_path, sampler):
    """A pass that ends paused must not stop the sampler twice, nor leave it
    paused for the next benchmark, which would then collect nothing."""
    from contextlib import ExitStack

    @mew.benchmark
    def bench_held(state):
        with ExitStack() as stack:
            for _ in state:
                stack.enter_context(state.pause())

    @mew.benchmark
    def bench_next(state):
        for _ in state:
            _spin()

    out = tmp_path / "o.json"
    mew.run(
        min_time=_SAMPLED_ITERATIONS,
        reporter=JSONReporter(output=out),
        profiler_manager=_sampled_manager(sampler),
    )
    rows = json.loads(out.read_text())["benchmarks"]
    assert [r["skipped"] for r in rows] == [False, False]
    assert "cpu_profile" in rows[1], "sampler collected nothing; raise the iteration count"


# --- Tachyon (Python 3.15+) --------------------------------------------------

_rng = random.Random(0)
_FLOATS = [_rng.random() for _ in range(50_000)]


def _hold_gil() -> list[float]:
    # ~15x `_spin`, all in C: sorted() holds the GIL and never reaches an
    # eval-breaker check, so an in-process sampler waits until it returns.
    return sorted(_FLOATS)


@pytest.mark.parametrize(
    ("sampler", "expected"),
    [
        ("auto", "TachyonManager" if sys.version_info >= (3, 15) else "PyinstrumentManager"),
        ("tachyon", "TachyonManager"),
        ("pyinstrument", "PyinstrumentManager"),
    ],
)
def test_cpu_manager_creates_the_chosen_sampler(monkeypatch, sampler, expected):
    from mew import cpu as _cpu

    made: list[tuple[str, tuple, dict]] = []
    for name in ("TachyonManager", "PyinstrumentManager"):
        monkeypatch.setattr(_cpu, name, lambda *a, _name=name, **kw: made.append((_name, a, kw)))
    _cpu.manager(2e-4, sampler=sampler, top_k=3)
    # pyinstrument reads no instructions, so it takes no top_k.
    kwargs = {"top_k": 3} if expected == "TachyonManager" else {}
    assert made == [(expected, (2e-4,), kwargs)]


def test_cpu_manager_rejects_an_unknown_sampler():
    from mew import cpu as _cpu

    with pytest.raises(ValueError, match="sampler must be one of"):
        _cpu.manager(sampler="perf")


@pytest.mark.skipif(sys.version_info >= (3, 15), reason="Tachyon is available")
def test_tachyon_manager_requires_python_3_15():
    from mew.cpu import TachyonManager

    with pytest.raises(SystemExit, match="3.15"):
        TachyonManager()


@needs_tachyon
def test_tachyon_weights_samples_by_time(tmp_path):
    """On GIL builds, samples during a GIL-holding C call are lost, and the one
    taken after it returns stands for the whole call. Counting samples would
    name `_spin` hottest; weighting each by the time since the previous one
    names `_hold_gil`."""
    from mew.cpu import TachyonManager

    @mew.benchmark
    def bench_mixed(state):
        for _ in state:
            _hold_gil()
            _spin()

    out = tmp_path / "out.json"
    # A sampler starved by a loaded machine leaves fewer, longer gaps; with 20
    # iterations a few of them could still put `_spin` ahead.
    mew.run(min_time="60x", reporter=JSONReporter(output=out), profiler_manager=TachyonManager())
    cpu = json.loads(out.read_text())["benchmarks"][0]["cpu_profile"]
    assert cpu["top_function"]["function"] == "_hold_gil"
    assert cpu["top_function"]["self_time"] > cpu["wall_time"] / 2


@needs_tachyon
def test_tachyon_skips_paused_regions(tmp_path):
    from mew.cpu import TachyonManager

    def paused_work() -> int:
        total = 0
        for i in range(60_000):
            total += i
        return total

    @mew.benchmark
    def bench_paused(state):
        for _ in state:
            with state.pause():
                paused_work()  # 3x the work of _spin, but never sampled
            _spin()

    out = tmp_path / "out.json"
    mew.run(
        min_time=_SAMPLED_ITERATIONS,
        reporter=JSONReporter(output=out),
        profiler_manager=TachyonManager(),
    )
    cpu = json.loads(out.read_text())["benchmarks"][0]["cpu_profile"]
    assert cpu["top_function"]["function"] == "_spin"


def _negate(x: float) -> float:
    return -x


@needs_tachyon
def test_tachyon_flame_graph_has_a_root_per_benchmark_and_native_frames(tmp_path):
    """`<native>` marks sorted() calling the key function. Opcodes are read only
    where a sample can land mid-instruction, i.e. on free-threaded builds."""
    from mew import cpu as _cpu

    @mew.benchmark
    def bench_spin(state):
        for _ in state:
            _spin()

    @mew.benchmark
    def bench_callback(state):
        for _ in state:
            # Already ordered, so the key calls take most of the time. On GIL
            # builds a short key phase before a long sort is never sampled.
            sorted(range(5_000), key=_negate)

    mgr = _cpu.TachyonManager()
    mew.run(
        min_time=_SAMPLED_ITERATIONS,
        reporter=JSONReporter(output=tmp_path / "o.json"),
        profiler_manager=mgr,
    )
    report = tmp_path / "cpu.html"
    _cpu.write_html(mgr, report)
    html = report.read_text(encoding="utf-8")
    assert "bench_spin" in html and "bench_callback" in html
    # Stacks are cut at the body: none of mew's own call chain above it.
    assert "runner.py" not in html
    assert '"<native>"' in html
    assert ('"opcodes"' in html) is not mew.gil_enabled()


# Far past the compact ints BINARY_OP_ADD_INT handles: each add copies ~6 KB, so
# `total += i` takes nearly all the time. Its lead must survive a starved sampler,
# whose few samples each stand for a long gap.
_BIG = 2**50_000


def _grow_big_int() -> int:
    total = _BIG
    for i in range(1_000):
        total += i
    return total


@needs_tachyon
@pytest.mark.skipif(mew.gil_enabled(), reason="needs a free-threaded build")
def test_tachyon_names_the_hottest_instruction_on_free_threaded_builds(tmp_path):
    from mew.cpu import TachyonManager

    @mew.benchmark
    def bench_big_int(state):
        for _ in state:
            _grow_big_int()

    out = tmp_path / "out.json"
    mew.run(
        min_time="100x", reporter=JSONReporter(output=out), profiler_manager=TachyonManager(top_k=2)
    )
    cpu = json.loads(out.read_text())["benchmarks"][0]["cpu_profile"]
    top = cpu["top_instructions"]
    # top_k caps the list. A starved sampler can land every sample on the
    # dominant add, leaving no second instruction to list.
    assert 1 <= len(top) <= 2
    assert {k: v for k, v in top[0].items() if k not in ("line", "share")} == {
        "instruction": "BINARY_OP",
        "source": "total += i",
        "file": "test_profile.py",
        "unspecialized": True,
    }
    assert isinstance(top[0]["line"], int)
    shares = [t["share"] for t in top]
    assert shares == sorted(shares, reverse=True) and shares[-1] > 0
    assert top[0]["share"] <= cpu["unspecialized_share"] <= 1
    assert cpu["unspecialized_share"] > 0.5


def test_instruction_source_slices_utf8_columns(tmp_path):
    from mew.cpu import _instruction_source

    src = tmp_path / "mod.py"
    src.write_text('label = "äöü"; total = left + right\nnext = (\n    1)\n', encoding="utf-8")
    first = src.read_text(encoding="utf-8").splitlines()[0].encode()
    col = first.index(b"left + right")
    # Column offsets count bytes: slicing the str would land two characters late.
    assert _instruction_source(str(src), (1, 1, col, col + 12)) == "left + right"
    # A span over several lines keeps its first line's part.
    assert _instruction_source(str(src), (2, 3, 7, 5)) == "( …"
    # Without columns (-X no_debug_ranges), the whole line.
    assert _instruction_source(str(src), (2, 2, -1, -1)) == "next = ("
    long = tmp_path / "long.py"
    long.write_text("x = " + " + ".join(["value"] * 20) + "\n", encoding="utf-8")
    assert len(_instruction_source(str(long), (1, 1, 4, 200))) == 40


# --- The cpu_profile contract --------------------------------------------------


def _conforms(value: Any, hint: Any) -> bool:
    """Whether JSON-decoded ``value`` has the type ``hint``, a TypedDict key for key."""
    if typing.is_typeddict(hint):
        fields = typing.get_type_hints(hint)
        return (
            isinstance(value, dict)
            and value.keys() == fields.keys()
            and all(_conforms(value[k], t) for k, t in fields.items())
        )
    if typing.get_origin(hint) in (typing.Union, types.UnionType):
        return any(_conforms(value, t) for t in typing.get_args(hint))
    if typing.get_origin(hint) is list:
        (item,) = typing.get_args(hint)
        return isinstance(value, list) and all(_conforms(v, item) for v in value)
    return type(value) is hint


@pytest.mark.parametrize("sampler", _SAMPLERS)
def test_samplers_summarize_the_hot_frame_in_one_shape(tmp_path, sampler):
    """Each CI interpreter runs this, so a key or type that only one sampler or
    CPython minor writes fails it: archives mixing them keep one schema."""
    from mew import cpu as _cpu

    mgr = _sampled_manager(sampler)

    @mew.benchmark
    def bench_hot(state):
        for _ in state:
            _spin()

    out = tmp_path / "out.json"
    switch_interval = sys.getswitchinterval()
    mew.run(min_time=_SAMPLED_ITERATIONS, reporter=JSONReporter(output=out), profiler_manager=mgr)

    cpu = json.loads(out.read_text())["benchmarks"][0].get("cpu_profile")
    assert cpu is not None, "sampler collected nothing; raise the iteration count"
    assert _conforms(cpu, _cpu.CpuProfile), cpu
    assert cpu["profiler"] == sampler
    top = cpu["top_function"]
    assert (top["function"], top["file"]) == ("_spin", "test_profile.py")
    assert 0 < top["self_time"] <= cpu["wall_time"]
    # Instructions are read on free-threaded Tachyon only, and then always.
    reads = getattr(mgr, "reads_instructions", False)
    assert bool(cpu["top_instructions"]) is reads
    assert (cpu["unspecialized_share"] is not None) is reads
    if sampler == "pyinstrument":
        # pyinstrument's line is the def line, so the summary leaves it out.
        assert top["line"] is None
    else:
        # The hottest line, not the def line.
        lines, start = inspect.getsourcelines(_spin)
        assert start < top["line"] < start + len(lines)
        # GIL builds lower the switch interval for the pass only.
        assert sys.getswitchinterval() == switch_interval


def test_conforms_rejects_drift():
    from mew.cpu import CpuProfile

    good = {
        "profiler": "tachyon",
        "wall_time": 0.5,
        "sample_count": 3,
        "top_function": {"function": "f", "file": None, "line": None, "self_time": 0.1},
        "top_instructions": [],
        "unspecialized_share": None,
    }
    assert _conforms(good, CpuProfile)
    assert not _conforms({**good, "extra": 1}, CpuProfile)
    assert not _conforms({k: v for k, v in good.items() if k != "unspecialized_share"}, CpuProfile)
    assert not _conforms({**good, "sample_count": 3.0}, CpuProfile)
    assert not _conforms({**good, "top_function": "f (b.py:1)"}, CpuProfile)
    assert not _conforms({**good, "top_instructions": [{"instruction": "NOP"}]}, CpuProfile)

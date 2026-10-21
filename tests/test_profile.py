"""In-process profiling: the Google Benchmark memory/profiler managers."""

from __future__ import annotations

import contextlib
import importlib
import inspect
import sys
from types import SimpleNamespace
from typing import Any, cast

import pytest
from _helpers import Capture

import mew
from mew.cpu import _opcode_tables

_MEMORY = {"peak_bytes": 1024, "total_bytes": 2048, "total_allocations": 5, "net_heap_growth": 512}
_REQUIRED_MEMORY = {"peak_bytes": 1024, "total_allocations": 5}
_PROFILE = {
    "profiler": "fake",
    "wall_time": 0.5,
    "sample_count": 500,
    "top_function": {"function": "foo", "file": "bar.py", "line": None, "self_time": 0.3},
    "top_instructions": [],
    "unspecialized_share": None,
}


class FakeMemoryManager:
    """Memory manager returning ``result`` from stop(), so the stamp path is
    testable without memray. ``calls`` logs each hook."""

    def __init__(self, result: Any = _MEMORY) -> None:
        self.result = result
        self.calls: list[str] = []

    def start(self) -> None:
        self.calls.append("start")

    def stop(self) -> Any:
        self.calls.append("stop")
        return self.result


class FakeProfilerManager:
    """Profiler manager returning ``result`` from get_result(), standing in for a
    sampler. ``calls`` logs each hook."""

    def __init__(self, result: Any = _PROFILE) -> None:
        self.result = result
        self.calls: list[str] = []

    def after_setup_start(self) -> None:
        self.calls.append("start")

    def before_teardown_stop(self) -> None:
        self.calls.append("stop")

    def pause(self) -> None:
        self.calls.append("pause")

    def resume(self) -> None:
        self.calls.append("resume")

    def get_result(self) -> Any:
        return self.result


class _RequiredHooksOnly:
    """A profiler manager without the optional get_result(), pause() and resume()."""

    def after_setup_start(self) -> None:
        pass

    def before_teardown_stop(self) -> None:
        pass


def _register_noop(**options: Any) -> None:
    """Register a benchmark that does nothing, for tests about the passes around it."""

    def bench_noop(state):
        for _ in state:
            pass

    mew.benchmark(**options)(bench_noop)


# --- manager registration and the Run stamp ----------------------------------


def test_registering_a_manager_replaces_the_previous_one():
    """Matches upstream GB, where registration is a plain replace."""
    from mew import _core

    _register_noop(iterations=2)
    first_mem, second_mem = FakeMemoryManager(), FakeMemoryManager()
    first_prof, second_prof = FakeProfilerManager(), FakeProfilerManager()
    _core.register_memory_manager(first_mem)
    _core.register_profiler_manager(first_prof)
    try:
        _core.register_memory_manager(second_mem)
        _core.register_profiler_manager(second_prof)
        mew.run(reporter=Capture())
    finally:
        _core.unregister_memory_manager()
        _core.unregister_profiler_manager()
    assert first_mem.calls == first_prof.calls == []
    assert second_mem.calls == second_prof.calls == ["start", "stop"]


@pytest.mark.parametrize(
    ("metrics", "block"),
    [
        (_MEMORY, {**_MEMORY, "iterations": 1, "allocations_per_iteration": 5.0}),
        # Optional keys the manager leaves out stay out of the row.
        (_REQUIRED_MEMORY, {**_REQUIRED_MEMORY, "iterations": 1, "allocations_per_iteration": 5.0}),
        # Nothing measured: no block rather than one of zeros.
        (None, None),
    ],
    ids=["all-keys", "required-keys", "none"],
)
def test_memory_manager_is_driven_and_stamped_onto_rows(metrics, block):
    """A memory manager is started and stopped once; its metrics, with `iterations` and
    `allocations_per_iteration`, become the row's `memory` block."""
    _register_noop(iterations=1)
    mgr = FakeMemoryManager(metrics)
    rep = Capture()
    mew.run(reporter=rep, memory_manager=mgr)

    assert mgr.calls == ["start", "stop"]
    # The pass's iterations come from Google Benchmark, and the per-iteration
    # rate is derived from them.
    assert rep.runs[0].get("memory") == block


@pytest.mark.parametrize("summary", [_PROFILE, {}], ids=["summary", "empty"])
def test_profiler_manager_is_driven_and_stamped_onto_rows(summary):
    """An empty summary is a summary too; only None leaves the block out."""
    _register_noop(iterations=1)
    mgr = FakeProfilerManager(summary)
    rep = Capture()
    mew.run(reporter=rep, profiler_manager=mgr)

    assert mgr.calls == ["start", "stop"]
    assert rep.runs[0]["cpu_profile"] == summary


def test_profiler_manager_result_keeps_its_json_types():
    """The summary crosses Google Benchmark as JSON text, so an int stays an int
    at any depth."""
    result = {"sample_count": 3, "flag": True, "missing": None, "top": [{"line": 7, "x": 0.5}]}
    _register_noop(iterations=1)
    rep = Capture()
    mew.run(reporter=rep, profiler_manager=FakeProfilerManager(result))

    cpu = rep.runs[0]["cpu_profile"]
    assert cpu == result
    assert type(cpu["sample_count"]) is type(cpu["top"][0]["line"]) is int
    assert cpu["flag"] is True


@pytest.mark.parametrize(
    "manager",
    [FakeProfilerManager(None), _RequiredHooksOnly()],
    ids=["returns-none", "required-hooks-only"],
)
def test_profiler_manager_without_a_result_leaves_row_unannotated(manager):
    """A profiler manager that returns `None` or has no `get_result()` leaves no `cpu_profile`."""

    @mew.benchmark(iterations=2)
    def bench_paused(state):
        for _ in state:
            with state.pause():  # pause() and resume() are optional hooks too
                pass

    rep = Capture()
    mew.run(reporter=rep, profiler_manager=manager)
    assert "cpu_profile" not in rep.runs[0]


@pytest.mark.parametrize("reuse_scope", [False, True])
def test_nested_pauses_toggle_the_profiler_only_at_the_outer_scope(reuse_scope):
    """A fresh inner scope does nothing; re-entering the outer one raises (see
    test_runner) without touching the profiler."""
    mgr = FakeProfilerManager()

    @mew.benchmark(iterations=2)
    def bench_nested(state):
        for _ in state:
            outer = state.pause()
            with outer:
                inner = outer if reuse_scope else state.pause()
                with contextlib.suppress(RuntimeError), inner:
                    pass
                mgr.calls.append("inner done")

    mew.run(reporter=Capture(), profiler_manager=mgr)
    # The timed run's pauses never reach the manager: there it would suspend
    # nothing, and worker threads would race on its state.
    timed, profiled = ["inner done"] * 2, ["pause", "inner done", "resume"] * 2
    assert mgr.calls == [*timed, "start", *profiled, "stop"]


def test_pause_held_across_the_loop_end_is_resumed_before_teardown():
    """mew resumes the profiler before stopping it, so a manager sees pause()
    and resume() in pairs; the scope's exit after the loop must not resume a
    manager whose pass is over."""
    mgr = FakeProfilerManager()

    @mew.benchmark(iterations=3)
    def bench_held(state):
        with contextlib.ExitStack() as stack:
            for _ in state:
                stack.enter_context(state.pause())

    mew.run(reporter=Capture(), profiler_manager=mgr)
    assert mgr.calls == ["start", "pause", "resume", "stop"]


def test_managers_do_not_leak_into_a_later_run():
    """Memory and profiler managers given to one `mew.run()` are not used by the next."""
    _register_noop(iterations=1)
    mem, prof = FakeMemoryManager(), FakeProfilerManager()
    mew.run(reporter=Capture(), memory_manager=mem, profiler_manager=prof)
    rep = Capture()
    mew.run(reporter=rep)

    # GB's manager registration is process-global; the run must unregister both.
    assert mem.calls == prof.calls == ["start", "stop"]
    assert "memory" not in rep.runs[0]
    assert "cpu_profile" not in rep.runs[0]


def test_memory_manager_stop_exception_propagates_out_of_run():
    """An exception from the memory manager's `stop()` propagates out of `mew.run()`."""

    class Exploding(FakeMemoryManager):
        def stop(self):
            raise RuntimeError("capture unreadable")

    _register_noop(iterations=1)
    with pytest.raises(RuntimeError, match="capture unreadable"):
        mew.run(reporter=Capture(), memory_manager=Exploding())


@pytest.mark.parametrize(
    ("kind", "result"),
    [
        ("memory", {"peak_bytes": 1024}),  # total_allocations is required
        ("memory", {**_MEMORY, "backend": "custom"}),
        ("memory", {**_REQUIRED_MEMORY, "peak_bytes": 1024.0}),
        ("memory", {**_REQUIRED_MEMORY, "total_bytes": "2048"}),  # optional keys too
        ("memory", [("peak_bytes", 1)]),
        ("profiler", {"top": [object()]}),  # json.dumps rejects it, at any depth
        ("profiler", {1: 2.0}),  # json.dumps would turn the key into "1"
        ("profiler", [("sample_count", 1)]),
    ],
)
def test_malformed_manager_result_propagates_out_of_run(kind, result):
    """A wrong-shaped manager result fails the run instead of being dropped silently."""
    _register_noop(iterations=1)
    fake = FakeMemoryManager if kind == "memory" else FakeProfilerManager
    kwargs: dict[str, Any] = {f"{kind}_manager": fake(result)}
    with pytest.raises(TypeError, match="must return"):
        mew.run(reporter=Capture(), **kwargs)


@pytest.mark.parametrize(("memory_iterations", "expected"), [(None, 16), (4, 4), (1000, 50)])
def test_memory_iterations_caps_the_memory_pass(memory_iterations, expected):
    """`memory_iterations` caps the memory pass (16 by default), never above the benchmark's own."""
    _register_noop(iterations=50)
    rep = Capture()
    mew.run(reporter=rep, memory_manager=FakeMemoryManager(), memory_iterations=memory_iterations)
    assert rep.runs[0]["memory"]["iterations"] == expected


def test_memory_pass_counts_batch_overshoot():
    """The memory pass counts whole batches: 4 requested iterations in batches of 10 count as 10."""

    @mew.benchmark(iterations=20)
    def bench_batch(state):
        for _ in state.batches(10):
            pass

    rep = Capture()
    mew.run(
        reporter=rep,
        memory_manager=FakeMemoryManager({"peak_bytes": 0, "total_allocations": 10}),
        memory_iterations=4,
    )
    mem = rep.runs[0]["memory"]
    assert (mem["iterations"], mem["allocations_per_iteration"]) == (10, 1.0)


@pytest.mark.skipif(
    mew.gil_enabled(),
    reason="threaded mode requires a free-threaded interpreter",
)
def test_manager_passes_run_a_single_thread():
    """As in Google Benchmark, the memory and profiler passes run thread 0 alone,
    and `state.threads` says so: a barrier sized by it must not wait for absent
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

    prof, rep = FakeProfilerManager(), Capture()
    mew.run(
        reporter=rep,
        memory_manager=FakeMemoryManager({"peak_bytes": 0, "total_allocations": 4}),
        memory_iterations=4,
        profiler_manager=prof,
    )
    # Timed run: both threads see 2. Each pass: thread 0 alone sees 1.
    assert sorted(seen[:2]) == [(0, 2), (1, 2)]
    assert seen[2:] == [(0, 1), (0, 1)]
    assert prof.calls == ["start", "stop"]
    mem = rep.runs[0]["memory"]
    assert (mem["iterations"], mem["allocations_per_iteration"]) == (4, 1.0)


@pytest.mark.parametrize("kind", ["memory", "profiler"])
def test_interrupted_pass_still_stops_the_manager(kind):
    """A `KeyboardInterrupt` in a memory or profiler pass still calls the manager's stop hook."""
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
        mew.run(reporter=Capture(), **kwargs)
    assert manager.calls == ["start", "stop"]


def test_failed_timed_runs_skip_the_manager_passes():
    """A benchmark whose timed run raises gets no memory or profiler pass, and neither block."""

    @mew.benchmark(iterations=4)
    def bench_raises(state):
        for _ in state:
            raise ValueError("boom")

    _register_noop(iterations=4)
    mem, prof, rep = FakeMemoryManager(), FakeProfilerManager(), Capture()
    mew.run(reporter=rep, memory_manager=mem, profiler_manager=prof)

    # Only bench_noop gets memory and profiler passes.
    assert mem.calls == prof.calls == ["start", "stop"]
    raises, ok = rep.runs
    assert "memory" not in raises and "cpu_profile" not in raises
    assert "memory" in ok and "cpu_profile" in ok


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


def test_memray_captures_the_loop_and_roots_its_flame_graph_at_the_body(tmp_path):
    """Setup allocations stay out of the figures. The flame graph puts back the
    body frame memray drops from a loop-scoped capture, which body-level
    allocations otherwise lack entirely."""
    pytest.importorskip("memray")
    from mew import memory as _memory

    setup_bytes = 50_000_000

    def nested_alloc_helper() -> bytearray:
        return bytearray(600_000)  # allocated one frame below the body

    @mew.benchmark
    def bench_rooted(state):
        fixture = bytearray(setup_bytes)  # setup: outside the capture window
        for _ in state:
            nested = nested_alloc_helper()
            inline = bytearray(400_000)  # allocated directly in the body frame
            del inline, nested
        del fixture

    rep, out = Capture(), tmp_path / "flame.html"
    with contextlib.ExitStack() as stack:
        manager = _memory.manager(stack)
        mew.run(min_time="20x", reporter=rep, memory_manager=manager)
        # Tagged with the user's benchmark frame, not a mew internal.
        assert [root[0] for _, root in manager.captures] == ["bench_rooted"]
        _memory.write_flamegraph(manager, out)

    mem = rep.runs[0]["memory"]
    assert mem["peak_bytes"] < setup_bytes / 10
    assert "total_bytes" not in mem  # read from the capture's metadata alone
    html = out.read_text()
    assert "bench_rooted" in html
    # A multi-frame stack renders through memray's real reporter, which pins the
    # attributes `_RootedRecord` must duck-type from `AllocationRecord`.
    assert "nested_alloc_helper" in html


def test_flamegraph_header_covers_every_capture(tmp_path, monkeypatch):
    """The combined report's header totals span all captures, not the first."""
    pytest.importorskip("memray")
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
    rep = Capture()
    with contextlib.ExitStack() as stack:
        manager = _memory.manager(stack)
        mew.run(reporter=rep, memory_manager=manager)
        _memory.write_flamegraph(manager, tmp_path / "flame.html")

    # Each row's figures are its capture's header.
    small, large = (row["memory"] for row in rep.runs)
    assert (
        rendered["m"].total_allocations == small["total_allocations"] + large["total_allocations"]
    )
    assert rendered["m"].peak_memory == large["peak_bytes"] > small["peak_bytes"]


def test_write_flamegraph_warns_when_nothing_was_captured(tmp_path, capsys):
    """`write_flamegraph()` writes no file and warns on stderr when memray captured nothing."""
    pytest.importorskip("memray")
    from mew import memory as _memory

    out = tmp_path / "flame.html"
    with contextlib.ExitStack() as stack:
        _memory.write_flamegraph(_memory.manager(stack), out)
    assert not out.exists()
    assert "no memory captures recorded" in capsys.readouterr().err


@pytest.mark.parametrize("sampler", _SAMPLERS)
def test_write_html_warns_when_nothing_was_sampled(tmp_path, capsys, sampler):
    """`write_html()` writes no file and warns on stderr when the sampler collected nothing."""
    from mew import cpu as _cpu

    out = tmp_path / "cpu.html"
    _cpu.write_html(_sampled_manager(sampler), out)
    assert not out.exists()
    assert "no CPU samples collected" in capsys.readouterr().err


def _sampling_nothing(sampler: str) -> Any:
    from mew import cpu as _cpu

    if sampler == "tachyon":
        mgr = _cpu.TachyonManager()
        mgr._unwinder = SimpleNamespace(get_stack_trace=list)  # finds no thread
        return mgr
    pytest.importorskip("pyinstrument")
    return _cpu.PyinstrumentManager(interval=60)  # its first sample is a minute away


@pytest.mark.parametrize("sampler", _SAMPLERS)
def test_sampler_reports_a_pass_without_samples(sampler):
    """A pass too short to sample keeps its row's cpu_profile, with zero samples
    and no hottest function."""
    mgr = _sampling_nothing(sampler)
    assert mgr.get_result() is None  # before the first pass

    _register_noop(iterations=2)
    rep = Capture()
    mew.run(reporter=rep, profiler_manager=mgr)
    cpu = rep.runs[0]["cpu_profile"]
    assert type(cpu.pop("wall_time")) is float
    assert cpu == {
        "profiler": sampler,
        "sample_count": 0,
        "top_function": None,
        "top_instructions": [],
        "unspecialized_share": None,
    }


@pytest.mark.parametrize("sampler", _SAMPLERS)
def test_pass_that_skips_before_its_loop_has_no_profile(sampler):
    """Not the profile of the benchmark before it, which the sampler last made."""
    _register_noop(iterations=2)
    calls = 0

    @mew.benchmark(iterations=2)
    def bench_skips_when_profiled(state):
        nonlocal calls
        calls += 1
        if calls > 1:  # the profiling pass, after the timed run
            state.skip_with_message("not when profiled")
            return
        for _ in state:
            pass

    rep = Capture()
    mew.run(reporter=rep, profiler_manager=_sampling_nothing(sampler))
    assert ["cpu_profile" in row for row in rep.runs] == [True, False]


def test_write_html_rejects_other_profiler_managers(tmp_path):
    """`write_html()` raises `TypeError` for a manager that is not one of mew's samplers."""
    from mew import cpu as _cpu

    with pytest.raises(TypeError, match="TachyonManager or PyinstrumentManager"):
        _cpu.write_html(cast("Any", FakeProfilerManager()), tmp_path / "cpu.html")


@pytest.mark.skipif(mew.gil_enabled(), reason="needs a free-threaded build with the GIL disabled")
def test_pyinstrument_is_rejected_before_import_on_free_threaded():
    """Importing pyinstrument would enable the GIL, which `_gil_stays_disabled`
    in conftest.py turns into a failure."""
    from mew.cpu import PyinstrumentManager

    with pytest.raises(RuntimeError, match="does not support free-threaded Python"):
        PyinstrumentManager()


@pytest.mark.parametrize(
    ("module", "create"),
    [
        pytest.param(
            "cpu",
            lambda mod: mod.PyinstrumentManager(),
            marks=pytest.mark.skipif(
                not mew.gil_enabled(), reason="pyinstrument is rejected before the lookup"
            ),
        ),
        ("memory", lambda mod: mod.manager(contextlib.ExitStack())),
    ],
)
def test_missing_profiler_package_raises_import_error(monkeypatch, module, create):
    """An exception with the install hint rather than an exit, so a caller such
    as the CLI picks the exit code."""
    mod = importlib.import_module(f"mew.{module}")
    monkeypatch.setattr(mod, "find_spec", lambda name: None)
    with pytest.raises(ImportError, match="Install it with"):
        create(mod)


def test_hottest_frame_excludes_pyinstrument_internals(tmp_path):
    """`_hottest_frame` skips pyinstrument's own frames, and is `None` when only those remain."""
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


@pytest.mark.parametrize(
    ("factory", "kwargs"),
    [
        ("PyinstrumentManager", {"interval": 0}),
        ("PyinstrumentManager", {"interval": float("nan")}),
        ("TachyonManager", {"interval": 0}),
        ("TachyonManager", {"interval": float("nan")}),
        ("TachyonManager", {"top_k": 0}),
        ("TachyonManager", {"top_k": 2.5}),
        ("manager", {"sampler": "perf"}),
        # Checked for pyinstrument too, which reads no instructions.
        ("manager", {"top_k": 0, "sampler": "pyinstrument"}),
    ],
)
def test_cpu_profiling_rejects_invalid_arguments(factory, kwargs):
    """Checked before the sampler is needed, so on every interpreter."""
    from mew import cpu as _cpu

    name = next(iter(kwargs))  # the invalid one
    with pytest.raises(ValueError, match=name):
        getattr(_cpu, factory)(**kwargs)


@pytest.mark.parametrize(
    ("sampler", "expected"),
    [
        ("auto", "TachyonManager" if sys.version_info >= (3, 15) else "PyinstrumentManager"),
        ("tachyon", "TachyonManager"),
        ("pyinstrument", "PyinstrumentManager"),
    ],
)
def test_cpu_manager_creates_the_chosen_sampler(monkeypatch, sampler, expected):
    """`cpu.manager()` builds the chosen sampler (`auto` is Tachyon on 3.15+), passing `top_k` to
    Tachyon only."""
    from mew import cpu as _cpu

    made: list[tuple[str, float, dict]] = []
    for name in ("TachyonManager", "PyinstrumentManager"):
        monkeypatch.setattr(
            _cpu, name, lambda interval, _name=name, **kw: made.append((_name, interval, kw))
        )
    _cpu.manager(2e-4, sampler=sampler, top_k=3)
    # pyinstrument reads no instructions, so it takes no top_k.
    kwargs = {"top_k": 3} if expected == "TachyonManager" else {}
    assert made == [(expected, 2e-4, kwargs)]


# --- Tachyon (Python 3.15+) --------------------------------------------------


@pytest.mark.skipif(sys.version_info >= (3, 15), reason="Tachyon is available")
def test_tachyon_manager_requires_python_3_15():
    """`TachyonManager` raises `RuntimeError` before Python 3.15."""
    from mew.cpu import TachyonManager

    with pytest.raises(RuntimeError, match="3.15"):
        TachyonManager()


@needs_tachyon
def test_tachyon_raises_when_it_cannot_read_the_process(monkeypatch):
    """`TachyonManager` raises `RuntimeError`, chained to the `OSError`, when it cannot read the
    process."""
    from mew import cpu as _cpu

    def unreadable(pid):
        raise OSError("operation not permitted")

    monkeypatch.setattr(_cpu, "_unwinder", unreadable)
    with pytest.raises(RuntimeError, match="cannot sample this process") as info:
        _cpu.TachyonManager()
    assert isinstance(info.value.__cause__, OSError)


@needs_tachyon
def test_tachyon_skips_paused_regions():
    """Tachyon takes no samples inside `state.pause()`, so paused work is never the hottest."""
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

    rep = Capture()
    mew.run(min_time=_SAMPLED_ITERATIONS, reporter=rep, profiler_manager=TachyonManager())
    assert rep.runs[0]["cpu_profile"]["top_function"]["function"] == "_spin"


@needs_tachyon
def test_tachyon_flame_graph_has_a_root_per_benchmark(tmp_path):
    """Stacks are cut at the body, so none of mew's own call chain above it shows."""
    from mew import cpu as _cpu

    @mew.benchmark
    def bench_first(state):
        for _ in state:
            _spin()

    @mew.benchmark
    def bench_second(state):
        for _ in state:
            _spin()

    mgr = _cpu.TachyonManager()
    mew.run(min_time=_SAMPLED_ITERATIONS, reporter=Capture(), profiler_manager=mgr)
    report = tmp_path / "cpu.html"
    _cpu.write_html(mgr, report)
    html = report.read_text(encoding="utf-8")
    assert "bench_first" in html and "bench_second" in html
    assert "runner.py" not in html


@pytest.mark.skipif(
    _opcode_tables() is None, reason="this interpreter has no table of specialized opcodes"
)
def test_instruction_summary_ranks_names_and_flags_instructions(tmp_path):
    """From fixed self times: on free-threaded builds, sampling and the
    specializer decide which instructions lead, so a sampled run can't pin this."""
    import opcode

    from mew.cpu import _summarize_instructions

    src = tmp_path / "mod.py"
    src.write_text("total += i\nx = y.z\n", encoding="utf-8")
    file, first, second = str(src), (1, 1, 0, 10), (2, 2, 4, 7)
    add_int = opcode._specialized_opmap["BINARY_OP_ADD_INT"]  # ty: ignore[unresolved-attribute]
    top, unspecialized = _summarize_instructions(
        {
            (file, second, opcode.opmap["LOAD_ATTR"]): 0.2,  # past top_k
            (file, first, opcode.opmap["BINARY_OP"]): 0.3,
            (file, first, add_int): 0.5,
        },
        total=2.0,  # the body's self time, of which instructions are a part
        top_k=2,
    )
    row = {"source": "total += i", "file": "mod.py", "line": 1}
    assert top == [
        {"instruction": "BINARY_OP_ADD_INT", **row, "share": 0.25, "unspecialized": False},
        {"instruction": "BINARY_OP", **row, "share": 0.15, "unspecialized": True},
    ]
    # Every generic instruction counts, also those past top_k.
    assert unspecialized == pytest.approx(0.25)
    assert _summarize_instructions({}, 1.0, 5) == ([], None)


def test_instruction_source_slices_utf8_columns(tmp_path):
    """`_instruction_source` slices by byte columns, keeps a multi-line span's first line, takes the
    whole line without columns, and cuts long source to 40 characters."""
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


@pytest.mark.parametrize("sampler", _SAMPLERS)
def test_samplers_summarize_the_hot_frame_in_one_shape(sampler):
    """Each CI interpreter runs this, so a key or type that only one sampler or
    CPython minor writes fails it: archives mixing them keep one schema."""
    from mew import cpu as _cpu

    mgr = _sampled_manager(sampler)

    @mew.benchmark
    def bench_hot(state):
        for _ in state:
            _spin()

    rep = Capture()
    switch_interval = sys.getswitchinterval()
    mew.run(min_time=_SAMPLED_ITERATIONS, reporter=rep, profiler_manager=mgr)

    cpu = rep.runs[0]["cpu_profile"]
    top = cpu["top_function"]
    assert top is not None, "sampler collected nothing; raise the iteration count"
    assert cpu.keys() == _cpu.CpuProfile.__required_keys__
    assert top.keys() == _cpu.TopFunction.__required_keys__
    assert type(cpu["sample_count"]) is int
    assert type(cpu["wall_time"]) is type(top["self_time"]) is float
    assert (cpu["profiler"], top["function"], top["file"]) == (sampler, "_spin", "test_profile.py")
    assert 0 < top["self_time"] <= cpu["wall_time"]
    # Instructions are read on free-threaded Tachyon only, and then always.
    reads = getattr(mgr, "reads_instructions", False)
    assert bool(cpu["top_instructions"]) is reads
    assert (cpu["unspecialized_share"] is not None) is reads
    for instruction in cpu["top_instructions"]:
        assert instruction.keys() == _cpu.TopInstruction.__required_keys__
    if sampler == "pyinstrument":
        # pyinstrument's line is the def line, so the summary leaves it out.
        assert top["line"] is None
    else:
        # The hottest line, not the def line.
        lines, start = inspect.getsourcelines(_spin)
        assert start < top["line"] < start + len(lines)
        # GIL builds lower the switch interval for the pass only.
        assert sys.getswitchinterval() == switch_interval

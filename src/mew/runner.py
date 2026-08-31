"""Bridge between the Python registry and the C++ runner."""

from __future__ import annotations

import socket
import sys
import warnings
from collections.abc import Iterable, Sequence
from contextlib import ExitStack
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import mew.context as _context
from mew import _core
from mew._console import overflow
from mew._registry import REGISTRY, Entry
from mew._session import new_session_id
from mew.machine import _silence_native_stderr, machine_context
from mew.reporter import Reporter

if TYPE_CHECKING:
    from mew._typing import BenchmarkOptions, BenchmarkResult


def _gil_enabled() -> bool:
    """True on a stock (GIL) interpreter, False on a free-threaded build."""
    return getattr(sys, "_is_gil_enabled", lambda: True)()


def _is_threaded(opts: BenchmarkOptions) -> bool:
    """Whether ``opts`` asks Google Benchmark to spawn more than one worker thread."""
    if (v := opts.get("threads")) is not None and v > 1:
        return True
    if (tr := opts.get("thread_range")) is not None:
        return max(tr) > 1
    if (tr := opts.get("dense_thread_range")) is not None:
        return tr[1] > 1
    return False


def _requested_threads(opts: BenchmarkOptions) -> int:
    """The thread count a threaded benchmark asked for (max of a range)."""
    if (v := opts.get("threads")) is not None:
        return int(v)
    if (tr := opts.get("thread_range")) is not None:
        return int(max(tr))
    if (tr := opts.get("dense_thread_range")) is not None:
        return int(tr[1])
    return 1


def _skipped_row(name: str, threads: int, message: str) -> BenchmarkResult:
    """A minimal ``skipped=True`` :class:`~mew._typing.BenchmarkResult` for a benchmark mew
    declined to run (never handed to Google Benchmark)."""
    return {
        "name": name,
        "run_name": name,
        "family_index": 0,
        "per_family_instance_index": 0,
        "run_type": "iteration",
        "aggregate_name": "",
        "repetitions": 0,
        "repetition_index": 0,
        "threads": threads,
        "iterations": 0,
        "real_time": 0.0,
        "cpu_time": 0.0,
        "real_accumulated_time": 0.0,
        "cpu_accumulated_time": 0.0,
        "time_unit": "ns",
        "label": "",
        "skipped": True,
        "skip_message": message,
        "counters": {},
    }


_FT_WARMED_UP = False


def _warmup_free_threading() -> None:
    """Force CPython's single→multi-thread transition on the main thread.

    On a free-threaded interpreter the first second-thread creation runs a
    stop-the-world immortalization pass. Google Benchmark spawns N raw worker
    threads that all ``PyGILState_Ensure`` at once; if that pass fires while
    they're mid-attach they deadlock. Driving it here, on a clean main thread
    before GB spawns anything, makes it a no-op by attach time. A no-op after the
    first call.
    """
    global _FT_WARMED_UP
    if _FT_WARMED_UP:
        return
    _FT_WARMED_UP = True
    import threading

    t = threading.Thread(target=lambda: None)
    t.start()
    t.join()


def _apply_options(handle: _core.BenchmarkHandle, opts: BenchmarkOptions) -> None:
    if (v := opts.get("min_time")) is not None:
        handle.min_time(float(v))
    if (v := opts.get("min_warmup_time")) is not None:
        handle.min_warmup_time(float(v))
    if (v := opts.get("iterations")) is not None:
        handle.iterations(int(v))
    if (v := opts.get("repetitions")) is not None:
        handle.repetitions(int(v))
    if v := opts.get("unit"):
        # Normalize the bare "ns"/"us"/... string (or a TimeUnit) to the enum;
        # the C++ binding takes a TimeUnit. TimeUnit(...) is idempotent on members.
        handle.unit(_core.TimeUnit(v))
    if opts.get("use_real_time"):
        handle.use_real_time()
    if opts.get("use_manual_time"):
        handle.use_manual_time()
    if opts.get("measure_process_cpu_time"):
        handle.measure_process_cpu_time()
    if opts.get("report_aggregates_only"):
        handle.report_aggregates_only(True)
    if (tr := opts.get("thread_range")) is not None:
        lo, hi = tr
        handle.thread_range(int(lo), int(hi))
    if (tr := opts.get("dense_thread_range")) is not None:
        lo, hi, stride = tr
        handle.dense_thread_range(int(lo), int(hi), int(stride))
    if (v := opts.get("threads")) is not None:
        handle.threads(int(v))


def _gb_argv(
    min_time: str | float | None,
    min_warmup_time: float | None,
    repetitions: int | None,
    random_interleaving: bool,
) -> list[str]:
    """Google Benchmark argv for the structured global knobs.

    Deliberately closed: per-benchmark knobs live on the decorators, benchmark
    selection is Python-side (:meth:`Registry.filter` / ``entries``), and GB's
    output/reporting flags would fight mew's own reporters. A new global knob
    earns a keyword on :func:`run`, not an argv passthrough.

    Every knob is emitted on every call, pinned to GB's own default when unset:
    GB flags are process-global, so a value parsed for one run would otherwise
    silently apply to every later :func:`run` in the same process (e.g.
    ``repetitions=2`` once → doubled rows forever after). Per-benchmark
    decorator options still win over these globals inside GB.
    """
    if min_time is None:
        mt = "0.5s"  # GB's default min time
    else:
        # A bare number means seconds, but GB deprecates the suffix-less form
        # (one "should have a suffix" line per benchmark on stderr): stamp the
        # `s`. Non-numeric strings ("100x", "0.5s") pass through untouched.
        mt = str(min_time)
        try:
            float(mt)
        except ValueError:
            pass
        else:
            mt += "s"
    return [
        "mew",
        f"--benchmark_min_time={mt}",
        f"--benchmark_min_warmup_time={min_warmup_time if min_warmup_time is not None else 0}",
        f"--benchmark_repetitions={repetitions if repetitions is not None else 1}",
        f"--benchmark_enable_random_interleaving={'true' if random_interleaving else 'false'}",
    ]


def run(
    entries: Sequence[Entry] | None = None,
    *,
    reporter: Reporter | Iterable[Reporter] | None = None,
    min_time: str | float | None = None,
    min_warmup_time: float | None = None,
    repetitions: int | None = None,
    random_interleaving: bool = False,
    session_tag: str | None = None,
    strict: bool = False,
    memory_manager: Any | None = None,
    profiler_manager: Any | None = None,
) -> int:
    """Run benchmarks via the C++ Google Benchmark backend.

    Each call is one *session*: a fresh time-ordered ``session_id`` is stamped into
    the reporter context, so result files stay addressable when several runs land in
    one archive.

    Parameters
    ----------
    entries : Sequence[Entry], optional
        Benchmarks to run. ``None`` runs the whole global registry; pass a filtered
        subset (e.g. from :meth:`Registry.filter`) to scope a run.
    reporter : Reporter, Iterable[Reporter], or None, optional
        A single reporter, an iterable of reporters (multiplexed via :class:`Fanout`),
        or ``None`` for Google Benchmark's default console reporter.
    min_time : str or float, optional
        Global minimum time per benchmark: seconds (``0.5``) or a fixed
        iteration count (``"100x"``). Per-benchmark ``min_time`` options win.
    min_warmup_time : float, optional
        Global warmup seconds per benchmark before measurement starts.
    repetitions : int, optional
        Repeat each benchmark this many times (variance metrics need >= 2).
    random_interleaving : bool, default False
        Randomly interleave repetitions across benchmarks to decorrelate
        thermal/load drift; effective with ``repetitions > 1``.
    session_tag : str, optional
        Human label for this session (e.g. ``"before"``), persisted next to
        ``session_id`` in the reporter context.
    strict : bool, default False
        Govern what happens when threaded benchmarks (``threads`` /
        ``thread_range`` / ``dense_thread_range``) are selected on a GIL interpreter,
        where they can't run
        (they would deadlock on Google Benchmark's start barrier). By default mew
        warns and skips them (emitting a ``skipped`` row per benchmark) and runs
        the rest, so a mixed suite still works on stock CPython. Set ``strict`` to
        raise a :class:`RuntimeError` instead, e.g. in CI where the threaded
        benchmarks are the point and a silent skip would mask a misconfiguration.
    memory_manager : object, optional
        A Google Benchmark memory manager (``start()`` / ``stop()``), e.g.
        :class:`mew.memory.MemrayManager`. Registered for the duration of the run;
        its figures land in each row's ``memory`` block.
    profiler_manager : object, optional
        A Google Benchmark profiler manager (``after_setup_start()`` /
        ``before_teardown_stop()``, optionally ``get_result()`` and
        ``pause()``/``resume()``), e.g. :class:`mew.cpu.PyinstrumentManager`.
        Registered for the duration of the run; its summary lands in each row's
        ``cpu_profile`` block.

    Returns
    -------
    int
        Number of benchmarks Google Benchmark executed; ``0`` if none were selected.
    """
    selected = list(entries) if entries is not None else REGISTRY.all()
    if not selected:
        return 0

    # Threaded mode can't run on a GIL build (it would deadlock on GB's start
    # barrier): warn and skip by default, raise under `strict`.
    skipped_rows: list[BenchmarkResult] = []
    threaded = [e for e in selected if _is_threaded(e.options)]
    if threaded and _gil_enabled():
        names = ", ".join(e.name for e in threaded[:3])
        more = overflow(len(threaded), 3)
        reason = (
            "threaded benchmarks require a free-threaded interpreter; "
            "this interpreter has the GIL enabled, where threaded mode "
            "would deadlock on Google Benchmark's start barrier"
        )
        if strict:
            raise RuntimeError(
                f"{reason}. Affected: {names}{more}. Run on a free-threaded build, "
                f"drop the threads option, or pass strict=False to skip them."
            )
        warnings.warn(
            f"skipping {len(threaded)} threaded benchmark(s) on a GIL interpreter "
            f"({names}{more}), run on a free-threaded build to execute them; {reason}.",
            RuntimeWarning,
            stacklevel=2,
        )
        skip_msg = f"skipped: {reason}"
        skipped_rows = [
            _skipped_row(e.name, _requested_threads(e.options), skip_msg) for e in threaded
        ]
        selected = [e for e in selected if not _is_threaded(e.options)]
        threaded = []  # filtered out of `selected`; none run this call

    rep = _to_single_reporter(reporter)

    extra_context: dict[str, Any] = {}
    if rep is not None:
        session: dict[str, Any] = {
            "id": new_session_id(),
            "date": datetime.now(UTC).isoformat(),
            "host": socket.gethostname(),
        }
        if session_tag:
            session["tag"] = session_tag
        # The machine provider is applied first so a suite can override it.
        extra_context["session"] = session
        extra_context["context"] = {**machine_context(), **_context._snapshot()}

    if not selected:
        # All skipped: GB emits no context for an empty registry, so drive the
        # reporter lifecycle here to surface the skipped rows.
        if rep is not None:
            try:
                rep.report_context(extra_context)
                if skipped_rows:
                    rep.report_runs(skipped_rows)
            finally:
                # Match Google Benchmark's normal reporter lifecycle: once
                # reporting starts, finalize even when a callback raises. This
                # closes owned sinks and terminates streamed JSON documents.
                if fn := getattr(rep, "finalize", None):
                    fn()
        return 0

    cli = _gb_argv(min_time, min_warmup_time, repetitions, random_interleaving)

    # Clear before registering so a second mew.run() in the same process
    # doesn't double-register entries.
    _core.clear_registered_benchmarks()
    for entry in selected:
        handle = _core.register_benchmark(entry.name, entry.fn)
        _apply_options(handle, entry.options)
        if entry.case_labels is not None:
            if entry.cases is None:
                handle.dense_range(0, len(entry.case_labels) - 1)
            else:
                # A name filter narrowed the family: register only those case
                # indices. The arg is the case index the trampoline reads via
                # state.range(0), so the right kwargs/label still bind.
                for i in entry.cases:
                    handle.arg(i)
            handle.arg_name("case")

    # Only reached on a free-threaded build (threaded entries are skipped above
    # under the GIL), where the warmup avoids the attach deadlock.
    if threaded:
        _warmup_free_threading()
    # Trigger GB's noisy system-info probes with fd 2 silenced, then run with
    # stderr live so user output and GB run-time diagnostics get through.
    with _silence_native_stderr():
        _core.preload_system_info()

    # GB keeps a raw pointer per manager, so each registration needs its pairing:
    # one left registered would silently profile the next run() in this process.
    with ExitStack() as stack:
        if memory_manager is not None:
            _core.register_memory_manager(memory_manager)
            stack.callback(_core.unregister_memory_manager)
        if profiler_manager is not None:
            _core.register_profiler_manager(profiler_manager)
            stack.callback(_core.unregister_profiler_manager)
        return _core.run_benchmarks(cli, rep, extra_context, skipped_rows)


def _to_single_reporter(
    reporter: Reporter | Iterable[Reporter] | None,
) -> Reporter | None:
    """Normalize the reporter argument to a single :class:`Reporter` or ``None``."""
    if reporter is None:
        return None
    # Anything with the reporter callbacks is a single reporter, even if iterable.
    if isinstance(reporter, Reporter):
        return reporter
    from mew.reporter import Fanout

    reps = list(reporter)
    if not reps:
        return None
    if len(reps) == 1:
        return reps[0]
    return Fanout(reps)

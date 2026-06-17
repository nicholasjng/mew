"""Bridge between the Python registry and the C++ runner."""

from __future__ import annotations

import os
import sys
import warnings
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

import mew._core as _core
import mew.context as _context
from mew._registry import REGISTRY, Entry
from mew._session import new_session_id
from mew.reporter import Reporter

if TYPE_CHECKING:
    from mew._typing import BenchmarkOptions, RunRow
    from mew.cpu import CPUProfile
    from mew.memory import MemoryProfile


def _gil_enabled() -> bool:
    """True on a stock (GIL) interpreter, False on a free-threaded build."""
    return getattr(sys, "_is_gil_enabled", lambda: True)()


def _is_threaded(opts: BenchmarkOptions) -> bool:
    """Whether ``opts`` asks Google Benchmark to spawn more than one worker thread."""
    if (v := opts.get("threads")) is not None and v > 1:
        return True
    if (tr := opts.get("thread_range")) is not None:
        return max(tr) > 1
    return False


def _requested_threads(opts: BenchmarkOptions) -> int:
    """The thread count a threaded benchmark asked for (max of a range)."""
    if (v := opts.get("threads")) is not None:
        return int(v)
    if (tr := opts.get("thread_range")) is not None:
        return int(max(tr))
    return 1


def _skipped_row(name: str, threads: int, message: str) -> RunRow:
    """A minimal ``skipped=True`` :class:`~mew._typing.RunRow` for a benchmark mew
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


@contextmanager
def _silence_native_stderr() -> Iterator[None]:
    """Redirect OS-level fd 2 to /dev/null within the scope.

    Google Benchmark's lazy system-info probes write platform diagnostics straight
    to fd 2, bypassing Python's ``sys.stderr``. Scope this narrowly (around
    ``_core.preload_system_info()``), never around the benchmark run itself:
    user benchmark bodies and GB's own run-time diagnostics (e.g. "Failed to
    match any benchmarks against regex") must stay visible.
    """
    sys.stderr.flush()
    devnull = os.open(os.devnull, os.O_WRONLY)
    saved = os.dup(2)
    try:
        os.dup2(devnull, 2)
        yield
    finally:
        sys.stderr.flush()
        os.dup2(saved, 2)
        os.close(saved)
        os.close(devnull)


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
    if (v := opts.get("threads")) is not None:
        handle.threads(int(v))


def _gb_argv(
    min_time: str | float | None,
    min_warmup_time: float | None,
    repetitions: int | None,
    random_interleaving: bool,
    filter: str | None,
) -> list[str]:
    """Google Benchmark argv for the structured global knobs.

    Deliberately closed: per-benchmark knobs live on the decorators, and GB's
    output/reporting flags would fight mew's own reporters. A new global knob
    earns a keyword on :func:`run`, not an argv passthrough.
    """
    argv = ["mew"]
    if min_time is not None:
        argv.append(f"--benchmark_min_time={min_time}")
    if min_warmup_time is not None:
        argv.append(f"--benchmark_min_warmup_time={min_warmup_time}")
    if repetitions is not None:
        argv.append(f"--benchmark_repetitions={repetitions}")
    if random_interleaving:
        argv.append("--benchmark_enable_random_interleaving=true")
    if filter:
        argv.append(f"--benchmark_filter={filter}")
    return argv


def run(
    entries: Sequence[Entry] | None = None,
    *,
    reporter: Reporter | Iterable[Reporter] | None = None,
    filter: str | None = None,
    min_time: str | float | None = None,
    min_warmup_time: float | None = None,
    repetitions: int | None = None,
    random_interleaving: bool = False,
    session_tag: str | None = None,
    strict: bool = False,
    memory_profiles: dict[str, MemoryProfile] | None = None,
    cpu_profiles: dict[str, CPUProfile] | None = None,
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
    filter : str, optional
        Google Benchmark name-filter regex applied to the registered names.
        Prefer :meth:`Registry.filter` plus ``entries`` for Python-side selection.
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
        ``thread_range``) are selected on a GIL interpreter, where they can't run
        (they would deadlock on Google Benchmark's start barrier). By default mew
        warns and skips them (emitting a ``skipped`` row per benchmark) and runs
        the rest, so a mixed suite still works on stock CPython. Set ``strict`` to
        raise a :class:`RuntimeError` instead, e.g. in CI where the threaded
        benchmarks are the point and a silent skip would mask a misconfiguration.
    memory_profiles, cpu_profiles : dict[str, MemoryProfile | CPUProfile], optional
        Out-of-loop profile results keyed by ``_profile_key``, attached onto each
        :class:`~mew._typing.RunRow`.

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
    skipped_rows: list[RunRow] = []
    threaded = [e for e in selected if _is_threaded(e.options)]
    if threaded and _gil_enabled():
        names = ", ".join(e.name for e in threaded[:3])
        more = f" (+{len(threaded) - 3} more)" if len(threaded) > 3 else ""
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

    rep = _to_single_reporter(reporter)
    # The binding merges extra_context into the GB context before calling
    # report_context, so every reporter sees session identity without a wrapper.
    extra_context: dict[str, Any] = {}
    if rep is not None:
        extra_context["session_id"] = new_session_id()
        if session_tag:
            extra_context["session_tag"] = session_tag
        if custom := _context._snapshot():
            extra_context["custom"] = custom
        # Projector turns the C++ live Runs into RunRow dicts and flushes the
        # skipped rows for `rep`.
        from mew._profile import _RunProjector

        rep = _RunProjector(
            rep,
            memory_profiles=memory_profiles,
            cpu_profiles=cpu_profiles,
            skipped_rows=skipped_rows,
        )

    if not selected:
        # All skipped: GB emits no context for an empty registry, so drive the
        # reporter lifecycle here to surface the skipped rows.
        if rep is not None:
            rep.report_context(extra_context)
            rep.finalize()
        return 0

    cli = _gb_argv(min_time, min_warmup_time, repetitions, random_interleaving, filter)

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
    if any(_is_threaded(e.options) for e in selected):
        _warmup_free_threading()
    # Trigger GB's noisy system-info probes with fd 2 silenced, then run with
    # stderr live so user output and GB run-time diagnostics get through.
    with _silence_native_stderr():
        _core.preload_system_info()
    return _core.run_benchmarks(cli, rep, extra_context)


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

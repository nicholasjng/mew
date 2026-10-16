"""Bridge between the Python registry and the C++ runner."""

from __future__ import annotations

import socket
import tempfile
import warnings
from collections.abc import Iterable, Sequence
from contextlib import ExitStack
from datetime import UTC, datetime
from math import isfinite
from typing import TYPE_CHECKING, Any

from mew import _core
from mew._capture import OutputCapture
from mew._console import overflow
from mew._registry import REGISTRY, Entry, case_names
from mew._session import new_session_id
from mew._typing import MemoryManager, ProfilerManager
from mew.context import get_context
from mew.machine import gil_enabled, machine_context
from mew.reporter import Fanout, Reporter, RichReporter

if TYPE_CHECKING:
    from mew._typing import BenchmarkOptions, BenchmarkResult


def _thread_counts(opts: BenchmarkOptions) -> tuple[int, ...]:
    """The thread counts a benchmark asked for; ``(1,)`` when unspecified."""
    v = opts.get("threads")
    if v is None:
        return (1,)
    return (v,) if isinstance(v, int) else tuple(v)


def _is_threaded(opts: BenchmarkOptions) -> bool:
    """Whether ``opts`` asks Google Benchmark to spawn more than one worker thread."""
    return max(_thread_counts(opts)) > 1


def _skipped_row(name: str, threads: int, message: str) -> BenchmarkResult:
    """A minimal ``skipped=True`` :class:`~mew._typing.BenchmarkResult` for a benchmark mew
    declined to run (never handed to Google Benchmark)."""
    return {
        "name": name,
        "run_name": name,
        "benchmark": name,
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
        "counters": [],
    }


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
        handle.unit(_core.TimeUnit(v))
    if opts.get("use_real_time"):
        handle.use_real_time()
    if opts.get("use_manual_time"):
        handle.use_manual_time()
    if opts.get("measure_process_cpu_time"):
        handle.measure_process_cpu_time()
    if opts.get("report_aggregates_only"):
        handle.report_aggregates_only(True)
    if opts.get("threads") is not None:
        # GB accumulates: one Threads() call per count runs the benchmark once each.
        for n in _thread_counts(opts):
            handle.threads(n)


_DEFAULT_MIN_TIME = "0.5s"
_DEFAULT_MEMORY_ITERATIONS = 16


def parse_min_time(value: str | float) -> str:
    """Validate seconds or a fixed iteration count and return Google Benchmark syntax."""
    text = str(value).strip()
    number = text[:-1] if text.endswith(("s", "x")) else text
    try:
        parsed = float(number)
    except ValueError:
        parsed = float("nan")
    if not isfinite(parsed) or parsed <= 0 or (text.endswith("x") and not number.isdigit()):
        raise ValueError(
            f"min_time must be positive seconds or an iteration count like '100x', got {value!r}"
        )
    return text if text.endswith(("s", "x")) else text + "s"


def _check_positive_int(value: int | None, name: str) -> None:
    if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 1):
        raise ValueError(f"{name} must be an integer >= 1, got {value!r}")


def _gb_argv(
    min_time: str | float | None,
    min_warmup_time: float | None,
    repetitions: int | None,
    random_interleaving: bool,
    memory_iterations: int | None = None,
) -> list[str]:
    """Validate the global run options and render them as Google Benchmark flags.

    Pure, so :func:`run` calls it before the run has any side effects. All values
    are emitted because Google Benchmark flags persist across runs in the same
    process. Decorator options take precedence.
    """
    _check_positive_int(repetitions, "repetitions")
    _check_positive_int(memory_iterations, "memory_iterations")
    if min_warmup_time is not None and (not isfinite(min_warmup_time) or min_warmup_time < 0):
        raise ValueError(f"min_warmup_time must be a finite number >= 0, got {min_warmup_time!r}")
    mt = parse_min_time(min_time) if min_time is not None else _DEFAULT_MIN_TIME
    mi = memory_iterations if memory_iterations is not None else _DEFAULT_MEMORY_ITERATIONS
    return [
        "mew",
        f"--benchmark_min_time={mt}",
        f"--benchmark_min_warmup_time={min_warmup_time if min_warmup_time is not None else 0}",
        f"--benchmark_repetitions={repetitions if repetitions is not None else 1}",
        f"--benchmark_enable_random_interleaving={'true' if random_interleaving else 'false'}",
        f"--benchmark_memory_iterations={mi}",
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
    memory_manager: MemoryManager | None = None,
    memory_iterations: int | None = None,
    profiler_manager: ProfilerManager | None = None,
    capture_output: bool = True,
) -> int:
    """Run the registered benchmarks.

    Each call is one session, whose time-ordered ID every row carries, so
    several runs can share a result file.

    Parameters
    ----------
    entries : Sequence[Entry], optional
        Benchmarks to run, e.g. from :meth:`Registry.filter`; ``None`` runs every
        registered benchmark.
    reporter : Reporter, Iterable[Reporter], or None, optional
        One reporter or several; ``None``, or none, prints mew's console table
        (:class:`~mew.reporter.RichReporter`).
    min_time : str or float, optional
        Minimum time per benchmark in seconds (``0.5``), or a fixed iteration
        count (``"100x"``). A benchmark's own ``min_time`` wins.
    min_warmup_time : float, optional
        Seconds each benchmark warms up before it is measured.
    repetitions : int, optional
        Runs per benchmark; statistics across runs need at least 2.
    random_interleaving : bool, default False
        Shuffle repetitions across benchmarks, so drift in load or temperature
        spreads over all of them. Needs ``repetitions > 1``.
    session_tag : str, optional
        A label for the session, e.g. ``"before"``, stored with its ID.
    strict : bool, default False
        Raise when threaded benchmarks are selected on a GIL build, where they
        can't run, instead of warning and reporting them as skipped: in CI, a
        skip can hide a misconfiguration.
    memory_manager : MemoryManager, optional
        Measures memory in an extra, untimed pass, e.g.
        :class:`mew.memory.MemrayManager`; its figures land in each row's
        ``memory`` block.
    memory_iterations : int, optional
        Iterations of the memory pass, at most the timed run's; 16 by default.
        Raise it to amortize one-time allocations in
        ``allocations_per_iteration``.
    profiler_manager : ProfilerManager, optional
        Profiles an extra, untimed pass, e.g. :class:`mew.cpu.TachyonManager`;
        its summary lands in each row's ``cpu_profile`` block.
    capture_output : bool, default True
        Hold back what benchmarks write to stdout and stderr, native code
        included, and print it to stderr after the run, so it can't break up
        the console table or JSON on stdout. Disable it to see output live,
        e.g. when debugging a crash, which would lose what was held back.

    Returns
    -------
    int
        How many benchmarks ran; ``0`` if none were selected.

    Raises
    ------
    ValueError
        If a run option is out of range.
    RuntimeError
        With ``strict``, if threaded benchmarks are selected on a GIL build.
    """
    cli = _gb_argv(min_time, min_warmup_time, repetitions, random_interleaving, memory_iterations)
    selected = list(entries) if entries is not None else REGISTRY.all()
    if not selected:
        return 0

    skipped_rows: list[BenchmarkResult] = []
    threaded = [e for e in selected if _is_threaded(e.options)]
    if threaded and gil_enabled():
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
            _skipped_row(e.name, max(_thread_counts(e.options)), skip_msg) for e in threaded
        ]
        selected = [e for e in selected if not _is_threaded(e.options)]
        threaded = []

    # Anything with the reporter callbacks is a single reporter, even if iterable.
    if isinstance(reporter, Reporter):
        reps = [reporter]
    else:
        reps = list(reporter) if reporter is not None else []
    if len(reps) > 1:
        rep = Fanout(reps)
    elif reps:
        rep = reps[0]
    else:
        rep = RichReporter(
            show_memory=memory_manager is not None,
            show_cpu=profiler_manager is not None,
            show_instructions=getattr(profiler_manager, "reads_instructions", False),
            show_label=any(e.case_labels for e in selected),
        )

    from mew import __version__  # deferred: mew imports this module

    session: dict[str, Any] = {
        "id": new_session_id(),
        "date": datetime.now(UTC).isoformat(),
        "host": socket.gethostname(),
        "mew_version": __version__,
    }
    if session_tag:
        session["tag"] = session_tag
    # Machine context first so a suite's providers can override it. It also loads
    # GB's CPU info, whose probes print to fd 2, silenced, before the run.
    session_context = {"session": session, "context": {**machine_context(), **get_context()}}

    if not selected:
        # All skipped: GB emits no context for an empty registry, so drive the
        # reporter lifecycle here to surface the skipped rows.
        try:
            rep.report_context(session_context)
            rep.report_runs(skipped_rows)
        finally:
            # As in GB's lifecycle, finalize even if a callback raised, so
            # owned sinks close and streamed JSON documents are terminated.
            if fn := getattr(rep, "finalize", None):
                fn()
        return 0

    # Clear before registering so a second mew.run() in the same process
    # doesn't double-register entries.
    _core.clear_registered_benchmarks()
    for entry in selected:
        if entry.case_labels is None:
            _apply_options(_core.register_benchmark(entry.name, entry.fn), entry.options)
            continue
        # One benchmark per case, named `name[label]`; the trampoline reads the
        # case index from state.range(0). A name filter may have narrowed the cases.
        for i, case_name in case_names(entry):
            if entry.cases is None or i in entry.cases:
                handle = _core.register_benchmark(case_name, entry.fn)
                _apply_options(handle, entry.options)
                handle.arg(i).arg_name("case")

    # On free-threaded builds the first second-thread creation runs a
    # stop-the-world pass that deadlocks GB's attaching workers; trigger it here.
    if threaded:
        _core.warmup_free_threading()

    # GB keeps a raw pointer per manager, so each registration needs its pairing:
    # one left registered would silently profile the next run() in this process.
    with ExitStack() as stack:
        if memory_manager is not None:
            _core.register_memory_manager(memory_manager)
            stack.callback(_core.unregister_memory_manager)
        if profiler_manager is not None:
            _core.register_profiler_manager(profiler_manager)
            stack.callback(_core.unregister_profiler_manager)
        capture = None
        if capture_output:
            capture = OutputCapture(stack.enter_context(tempfile.TemporaryFile()))
            stack.callback(capture.close)
        return _core.run_benchmarks(cli, rep, session_context, skipped_rows, capture)

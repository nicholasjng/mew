"""Run registered benchmarks through Google Benchmark.

Also captures what benchmarks print, native writes included, for the run's
duration (:class:`OutputCapture`).
"""

from __future__ import annotations

import functools
import os
import sys
import tempfile
import threading
import time
import uuid
import warnings
from collections.abc import Iterable, Mapping, Sequence
from contextlib import ExitStack
from datetime import UTC, datetime
from typing import Any, BinaryIO

from mew import _core
from mew._options import (
    check_count,
    check_min_warmup_time,
    check_run_name,
    check_strict,
    is_threaded,
    names_of,
    parse_min_time,
    threaded_on_gil,
)
from mew._registry import REGISTRY, Entry, longest_row_name, selected_cases
from mew._results import encode_context
from mew._typing import MemoryManager, ProfilerManager
from mew.machine import machine_context
from mew.reporter import Reporter, RichReporter, _Fanout

__all__ = ["run"]


_last_run_id = 0
_run_id_lock = threading.Lock()


def _new_run_id() -> str:
    """Return a time-ordered UUIDv7 string, later than any this process made before.

    Sub-millisecond time fills ``rand_a`` (RFC 9562, method 3). An id that
    wouldn't sort after the previous one takes its successor instead, so runs
    that start within one clock tick, such as two short runs that append to
    one file, keep their order in ``mew log``.
    """
    global _last_run_id
    unix_ms, sub_ms_ns = divmod(time.time_ns(), 1_000_000)
    rand_a = sub_ms_ns * 4096 // 1_000_000
    rand_b = int.from_bytes(os.urandom(8)) & 0x3FFF_FFFF_FFFF_FFFF
    value = (
        ((unix_ms & 0xFFFF_FFFF_FFFF) << 80)  # unix_ts_ms
        | (0x7 << 76)  # version 7
        | (rand_a << 64)
        | (0b10 << 62)  # RFC 4122/9562 variant
        | rand_b
    )
    with _run_id_lock:
        value = max(value, _last_run_id + 1)
        _last_run_id = value
    return str(uuid.UUID(int=value))


_DEFAULT_MIN_TIME = "0.5s"


def _gb_argv(
    min_time: str | float | None,
    min_warmup_time: float | None,
    repetitions: int | None,
    random_interleaving: bool,
) -> list[str]:
    """Validate the global run options and render them as Google Benchmark flags.

    Pure, so :func:`run` calls it before the run has any side effects. All values
    are emitted because Google Benchmark flags persist across runs in the same
    process. Decorator options take precedence, except that an ``Nx`` min_time
    also overrides a decorator's min_time.
    """
    if repetitions is not None:
        check_count(repetitions, "repetitions")
    if min_warmup_time is not None:
        check_min_warmup_time(min_warmup_time)
    mt = parse_min_time(min_time) if min_time is not None else _DEFAULT_MIN_TIME
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
    run_name: str | None = None,
    context: Mapping[str, Any] | None = None,
    strict: bool = False,
    memory_manager: MemoryManager | None = None,
    profiler_manager: ProfilerManager | None = None,
    capture_output: bool = True,
) -> int:
    """Run benchmarks, by default every registered one.

    Each call is one run, whose time-ordered ID every row carries, so several
    runs can share a result file.

    Parameters
    ----------
    entries : Sequence[Entry], optional
        Benchmarks to run, e.g. from :meth:`Registry.filter`. ``None`` runs every
        registered benchmark.
    reporter : Reporter, Iterable[Reporter], or None, optional
        One reporter or several. ``None`` prints mew's console table
        (:class:`~mew.reporter.RichReporter`).
    min_time : str or float, optional
        Minimum time per benchmark (``0.5``, ``"0.5s"``, ``"500ms"``, ``"1m"``),
        or a fixed iteration count (``"100x"``). A benchmark's own
        ``iterations`` wins over both, its own ``min_time`` only over a time.
    min_warmup_time : float, optional
        Seconds each benchmark warms up before it is measured.
    repetitions : int, optional
        Runs per benchmark. Statistics across runs need at least 2.
    random_interleaving : bool, default False
        Shuffle repetitions across benchmarks, so drift in load or temperature
        spreads over all of them. Needs ``repetitions > 1``.
    run_name : str, optional
        A name for the run, e.g. ``"before"``, stored with its ID. ``mew log``
        and ``mew compare`` show it.
    context : Mapping[str, Any], optional
        What to record about the run beside the machine, e.g. a dataset or a
        commit, in every row's ``context``. Its keys win over mew's own. Nested
        mappings and lists are kept. A value JSON can't hold is stored as its
        type's qualified name, a path as its string.
    strict : bool, default False
        Raise when threaded benchmarks are selected on a GIL build, where they
        can't run, instead of warning and leaving them out of the results: in
        CI, that can hide a misconfiguration.
    memory_manager : MemoryManager, optional
        Measures memory in an extra, untimed pass of at most 16 iterations, as
        in Google Benchmark, e.g. :class:`mew.memory.MemrayManager`. Its
        figures land in each row's ``memory`` block.
    profiler_manager : ProfilerManager, optional
        Profiles an extra, untimed pass, e.g. :class:`mew.cpu.TachyonManager`.
        Its summary lands in each row's ``cpu_profile`` block.
    capture_output : bool, default True
        Hold back what benchmarks write to stdout and stderr, native code
        included, and print it to stderr after the run, so it can't break up
        the console table or JSON on stdout. Disable it to see output live,
        e.g. when debugging a crash, which would lose what was held back.

    Returns
    -------
    int
        How many benchmarks ran, ``0`` if none were selected.

    Raises
    ------
    TypeError
        If a run option has the wrong type.
    ValueError
        If a run option is out of range, or ``run_name`` is empty.
    RuntimeError
        With ``strict``, if threaded benchmarks are selected on a GIL build.
    """
    cli = _gb_argv(min_time, min_warmup_time, repetitions, random_interleaving)
    if run_name is not None:
        check_run_name(run_name)
    if context is not None and (
        not isinstance(context, Mapping) or not all(isinstance(k, str) for k in context)
    ):
        raise TypeError(f"context must be a mapping with string keys, got {context!r}")
    selected = list(entries) if entries is not None else REGISTRY.all()
    if not selected:
        return 0

    if strict:
        check_strict(selected)
    skipped, count = threaded_on_gil(selected)
    if skipped:
        # Dropped without rows: they never ran, so they have nothing to report.
        warnings.warn(
            f"skipped {count} on a GIL build: {names_of(skipped)}\n"
            "hint: threads need a free-threaded interpreter",
            RuntimeWarning,
            stacklevel=2,
        )
        selected = [e for e in selected if not is_threaded(e.options)]
        if not selected:
            return 0
    threaded = any(is_threaded(e.options) for e in selected)

    # Anything with the reporter callbacks is a single reporter, even if iterable.
    if isinstance(reporter, Reporter):
        reps = [reporter]
    else:
        reps = list(reporter) if reporter is not None else []
    if len(reps) > 1:
        rep = _Fanout(reps)
    elif reps:
        rep = reps[0]
    else:
        rep = RichReporter(
            show_memory=memory_manager is not None,
            show_cpu=profiler_manager is not None,
            name_width=longest_row_name(selected),
        )

    from mew import __version__  # deferred: mew imports this module

    run: dict[str, Any] = {
        "id": _new_run_id(),
        # Fixed width (UTC, milliseconds, "Z"), so dates sort as strings.
        "date": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
    }
    if run_name is not None:
        run["name"] = run_name
    # The mew that ran it and the machine first, so a suite's own values can
    # override them. machine_context() also loads GB's CPU info, whose probes
    # print to fd 2, silenced, before the run.
    provenance = {"mew": __version__, **machine_context(), **encode_context(context or {})}
    run_context = {"run": run, "context": provenance}

    # Clear before registering so a second mew.run() in the same process
    # doesn't double-register entries.
    _core.clear_registered_benchmarks()
    # A family case's parameters by its registered name, for its rows' `params`.
    params: dict[str, dict[str, Any]] = {}
    for entry in selected:
        if entry.case_labels is None:
            _core.register_benchmark(entry.name, entry.fn, dict(entry.options))
            continue
        # One benchmark per case, named `name[label]`, its index bound for the
        # trampoline. A name filter may have narrowed the cases.
        for i, case_name in selected_cases(entry):
            fn = functools.partial(entry.fn, case=i)
            _core.register_benchmark(case_name, fn, dict(entry.options))
            if entry.case_params is not None:
                params[case_name] = entry.case_params[i]

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
        return _core.run_benchmarks(cli, rep, run_context, params, capture)


class OutputCapture:
    """Redirect fds 1 and 2 into ``file`` until :meth:`close`; the caller owns ``file``."""

    def __init__(self, file: BinaryIO) -> None:
        self._file = file
        self._saved = (os.dup(1), os.dup(2))
        self.resume()

    def resume(self) -> None:
        sys.stdout.flush()
        sys.stderr.flush()
        os.dup2(self._file.fileno(), 1)
        os.dup2(self._file.fileno(), 2)

    def suspend(self) -> None:
        sys.stdout.flush()
        sys.stderr.flush()
        os.dup2(self._saved[0], 1)
        os.dup2(self._saved[1], 2)

    def close(self) -> None:
        """Restore the streams, then print everything captured to stderr."""
        # Suspending again when already suspended is a no-op.
        self.suspend()
        for fd in self._saved:
            os.close(fd)
        self._file.seek(0)
        if captured := self._file.read().decode(errors="replace"):
            sys.stderr.write(f"\noutput from benchmarks:\n{captured}")
            sys.stderr.flush()

"""Bridge between the Python registry and the C++ runner."""

from __future__ import annotations

import os
import sys
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

import mew._core as _core
import mew.context as _context
from mew._registry import REGISTRY, Entry
from mew._session import new_session_id
from mew.reporter import Reporter

if TYPE_CHECKING:
    from mew._typing import BenchmarkOptions
    from mew.cpu import CPUProfile
    from mew.memory import MemoryProfile


@contextmanager
def _silence_native_stderr() -> Iterator[None]:
    """Redirect OS-level fd 2 to /dev/null around the C++ call.

    Google Benchmark's init writes platform diagnostics straight to fd 2, bypassing
    Python's ``sys.stderr``. User-facing errors route through the reporter callback
    or as Python exceptions, not fd 2.
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
        handle.unit(v)
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


def run(
    entries: Sequence[Entry] | None = None,
    *,
    argv: Sequence[str] | None = None,
    reporter: Reporter | Iterable[Reporter] | None = None,
    filter: str | None = None,
    session_tag: str | None = None,
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
    argv : Sequence[str], optional
        Argv forwarded to Google Benchmark's ``Initialize``. Defaults to ``["mew"]``.
    reporter : Reporter, Iterable[Reporter], or None, optional
        A single reporter, an iterable of reporters (multiplexed via :class:`Fanout`),
        or ``None`` for Google Benchmark's default console reporter.
    filter : str, optional
        Regex forwarded to Google Benchmark as ``--benchmark_filter=``.
    session_tag : str, optional
        Human label for this session (e.g. ``"before"``), persisted next to
        ``session_id`` in the reporter context.
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

    cli = list(argv) if argv is not None else ["mew"]
    if filter:
        cli.append(f"--benchmark_filter={filter}")

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
        # Projector turns the C++ live Runs into RunRow dicts for `rep`.
        from mew._profile import _RunProjector

        rep = _RunProjector(rep, memory_profiles=memory_profiles, cpu_profiles=cpu_profiles)
    with _silence_native_stderr():
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

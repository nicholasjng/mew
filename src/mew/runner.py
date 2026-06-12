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
    from mew._core import Run
    from mew._typing import BenchmarkOptions


@contextmanager
def _silence_native_stderr() -> Iterator[None]:
    """Redirect OS-level fd 2 to /dev/null around the C++ call.

    Google Benchmark's init writes platform diagnostics straight to fd 2, bypassing Python's ``sys.stderr``.
    User-facing benchmark errors route through the reporter callback or as Python exceptions, not fd 2.
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


def run(
    entries: Sequence[Entry] | None = None,
    *,
    argv: Sequence[str] | None = None,
    reporter: Reporter | Iterable[Reporter] | None = None,
    filter: str | None = None,
    session_tag: str | None = None,
) -> int:
    """Run benchmarks via the C++ Google Benchmark backend.

    Each call is one *session*: a fresh time-ordered ``session_id`` is stamped
    into the reporter context, so result files stay addressable when several
    runs land in one archive.

    Parameters
    ----------
    entries : Sequence[Entry], optional
        Benchmarks to run.
        ``None`` runs everything in the global registry; pass a filtered subset (e.g. from :meth:`Registry.filter`) to scope a run.
    argv : Sequence[str], optional
        Argv forwarded to Google Benchmark's ``Initialize``.
        Defaults to ``["mew"]``.
    reporter : Reporter, Iterable[Reporter], or None, optional
        A single reporter, an iterable of reporters (multiplexed via :class:`Fanout`), or ``None`` for Google Benchmark's default console reporter.
    filter : str, optional
        Regex forwarded to Google Benchmark as ``--benchmark_filter=``.
    session_tag : str, optional
        Human label for this session (e.g. ``"before"``), persisted next to
        ``session_id`` in the reporter context.

    Returns
    -------
    int
        Number of benchmarks Google Benchmark executed.
        ``0`` if no entries were selected.
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
                # indices. The arg value is the case index the trampoline reads
                # via state.range(0), so the right kwargs/label still bind.
                for i in entry.cases:
                    handle.arg(i)
            handle.arg_name("case")

    rep = _to_single_reporter(reporter)
    if rep is not None:
        rep = _ContextInjecting(
            rep,
            custom=_context._snapshot(),
            session_id=new_session_id(),
            session_tag=session_tag,
        )
    with _silence_native_stderr():
        return _core.run_benchmarks(cli, rep)


def _to_single_reporter(
    reporter: Reporter | Iterable[Reporter] | None,
) -> Reporter | None:
    """Normalize the reporter argument to a single :class:`Reporter` or ``None``."""
    if reporter is None:
        return None
    # Treat anything with the reporter callbacks as a single reporter, even if
    # it happens to also be iterable.
    if isinstance(reporter, Reporter):
        return reporter
    from mew.reporter import Fanout

    reps = list(reporter)
    if not reps:
        return None
    if len(reps) == 1:
        return reps[0]
    return Fanout(reps)


class _ContextInjecting:
    """Reporter wrapper that injects session identity and user context.

    ``session_id``/``session_tag`` land as top-level context keys; user context
    goes under ``ctx['custom']`` (only when non-empty, preserving the bare
    GB-context shape for runs that never call :func:`mew.set_context`).
    """

    def __init__(
        self,
        inner: Reporter,
        *,
        custom: dict[str, Any],
        session_id: str,
        session_tag: str | None = None,
    ) -> None:
        self._inner = inner
        self._custom = custom
        self._session_id = session_id
        self._session_tag = session_tag

    def report_context(self, context: dict[str, Any]) -> bool:
        merged = dict(context)
        merged["session_id"] = self._session_id
        if self._session_tag:
            merged["session_tag"] = self._session_tag
        if self._custom:
            merged["custom"] = self._custom
        return self._inner.report_context(merged)

    def report_runs(self, runs: list[Run]) -> None:
        self._inner.report_runs(runs)

    def finalize(self) -> None:
        fn = getattr(self._inner, "finalize", None)
        if callable(fn):
            fn()

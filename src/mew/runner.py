"""Bridge between the Python registry and the C++ runner."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from mew import _core
from mew import context as _context
from mew._registry import REGISTRY, Entry


def _apply_options(handle: _core.BenchmarkHandle, opts: dict[str, Any]) -> None:
    if (v := opts.get("min_time")) is not None:
        handle.min_time(float(v))
    if (v := opts.get("min_warmup_time")) is not None:
        handle.min_warmup_time(float(v))
    if (v := opts.get("iterations")) is not None:
        handle.iterations(int(v))
    if (v := opts.get("repetitions")) is not None:
        handle.repetitions(int(v))
    if v := opts.get("unit"):
        handle.unit(str(v))
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
    reporter: Any = None,
    filter: str | None = None,
) -> int:
    """Run benchmarks via the C++ Google Benchmark backend.

    With `entries=None`, runs everything in the global registry. Pass a
    filtered subset (e.g. from `REGISTRY.filter(pattern)`) to scope a run.
    `reporter` is a single reporter or any iterable of reporters; multiple
    reporters are multiplexed via `Fanout`. `filter` is forwarded to GB as
    `--benchmark_filter=` (a regex).
    """
    selected = list(entries) if entries is not None else REGISTRY.all()
    if not selected:
        return 0

    cli = list(argv) if argv is not None else ["mew"]
    if filter:
        cli.append(f"--benchmark_filter={filter}")

    for entry in selected:
        handle = _core.register_benchmark(entry.name, entry.fn)
        _apply_options(handle, entry.options)

    rep = _to_single_reporter(reporter)
    custom = _context._snapshot()
    if custom and rep is not None:
        rep = _ContextInjecting(rep, custom)
    return _core.run_benchmarks(cli, rep)


def _to_single_reporter(reporter: Any) -> Any:
    """Accept a reporter, an iterable of reporters, or None."""
    if reporter is None:
        return None
    # Treat anything with the reporter callbacks as a single reporter, even if
    # it happens to also be iterable.
    if hasattr(reporter, "report_runs"):
        return reporter
    from mew.reporter import Fanout

    reps = list(reporter)
    if not reps:
        return None
    if len(reps) == 1:
        return reps[0]
    return Fanout(reps)


class _ContextInjecting:
    """Reporter wrapper that injects user-defined context under ctx['custom']."""

    def __init__(self, inner: Any, custom: dict[str, Any]) -> None:
        self._inner = inner
        self._custom = custom

    def report_context(self, context: dict[str, Any]) -> bool:
        merged = dict(context)
        merged["custom"] = self._custom
        return self._inner.report_context(merged)

    def report_runs(self, runs: Any) -> None:
        self._inner.report_runs(runs)

    def finalize(self) -> None:
        fn = getattr(self._inner, "finalize", None)
        if callable(fn):
            fn()

"""Shared infrastructure for profile-based extensions (memory, CPU, ...).

``_ProfileState`` runs a benchmark body outside Google Benchmark's iteration loop.
``_RunProjector`` wraps a reporter: it projects each C++ ``Run`` to a ``RunRow``
(the single binding-boundary projection) and attaches memory/CPU profiles onto
the row by benchmark name.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, nullcontext
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from mew._core import Run
    from mew._registry import Entry
    from mew._typing import RunRow
    from mew.cpu import CPUProfile
    from mew.memory import MemoryProfile
    from mew.reporter import Reporter


def _profile_key(function_name: str, args: str) -> str:
    """Profile lookup key from GB's *structured* name parts.

    Survives the ``/min_time:…``/aggregate suffixes on ``benchmark_name()``;
    yields ``entry.name/case:<i>`` for a family, ``entry.name`` otherwise.
    """
    return f"{function_name}/{args}" if args else function_name


def iter_entry_cases(entry: Entry) -> Iterator[tuple[str, int]]:
    """Yield ``(profile_key, range_value)`` per case an entry expands to.

    Families yield one pair per case so a profiler drives each variant via
    ``_ProfileState(range_value=i)`` under the key the reporter looks up. A
    family narrowed by a ``-k`` filter (``entry.cases``) yields only those cases.
    """
    if entry.case_labels is None:
        yield _profile_key(entry.name, ""), 0
    else:
        indices = entry.cases if entry.cases is not None else range(len(entry.case_labels))
        for i in indices:
            yield _profile_key(entry.name, f"case:{i}"), i


class _ProfileState:
    """``_core.State`` stand-in for the out-of-loop profiling passes.

    Runs the body ``n_iterations`` times (memory: 1; sampling CPU: many).
    ``range_value`` feeds ``range(0)`` so a family trampoline runs per case.
    ``pause`` is a context-manager factory (CPU injects one that suspends sampling);
    ``None`` makes :meth:`pause` a no-op, so memory still measures the region.
    ``on_loop_start`` / ``on_loop_end`` fire once when the body enters and leaves
    its ``for _ in state`` loop, so a profiler can scope its capture to the timed
    region and exclude fixture/setup work.
    """

    range_size: int = 0
    threads: int = 1
    thread_index: int = 0
    name: str = ""
    skipped: bool = False
    error_occurred: bool = False

    def __init__(
        self,
        n_iterations: int = 1,
        range_value: int = 0,
        pause: Callable[[], AbstractContextManager[None]] | None = None,
        on_loop_start: Callable[[], None] | None = None,
        on_loop_end: Callable[[], None] | None = None,
    ) -> None:
        self._n = n_iterations
        self._i = 0
        self._range = range_value
        self._pause = pause
        self._on_loop_start = on_loop_start
        self._on_loop_end = on_loop_end
        self._loop_started = False
        self._loop_ended = False

    def _loop_begin(self) -> None:
        if not self._loop_started:
            self._loop_started = True
            if self._on_loop_start is not None:
                self._on_loop_start()

    def _loop_finish(self) -> None:
        if self._loop_started and not self._loop_ended:
            self._loop_ended = True
            if self._on_loop_end is not None:
                self._on_loop_end()

    @property
    def iterations(self) -> int:
        return self._n

    @property
    def max_iterations(self) -> int:
        return self._n

    def __iter__(self) -> _ProfileState:
        return self

    def __next__(self) -> None:
        if self._i >= self._n:
            self._loop_finish()
            raise StopIteration
        self._loop_begin()
        self._i += 1

    def keep_running_batch(self, n: int) -> bool:
        if self._i < self._n:
            self._loop_begin()
            self._i += n
            return True
        self._loop_finish()
        return False

    def batches(self, n: int) -> Iterator[int]:
        while self.keep_running_batch(n):
            yield n

    def pause(self) -> AbstractContextManager[None]:
        # Injected factory (CPU suspends sampling); None → no-op (memory measures setup).
        return self._pause() if self._pause is not None else nullcontext()

    def set_counter(self, name: str, value: float) -> None:
        pass

    def set_label(self, label: str) -> None:
        pass

    def set_items_processed(self, n: int) -> None:
        pass

    def set_bytes_processed(self, n: int) -> None:
        pass

    def set_iteration_time(self, seconds: float) -> None:
        pass

    def skip_with_error(self, msg: str) -> None:
        pass

    def skip_with_message(self, msg: str) -> None:
        pass

    def range(self, pos: int = 0) -> int:
        return self._range


class _RunProjector:
    """Reporter wrapper: project each C++ ``Run`` to a ``RunRow``, attaching profiles.

    :func:`mew.run` always wraps the user's reporter in one, so reporters only
    see :class:`~mew._typing.RunRow` dicts. Memory/CPU profiles are attached onto
    the row, not the ``Run``.
    """

    def __init__(
        self,
        inner: Reporter,
        *,
        memory_profiles: dict[str, MemoryProfile] | None = None,
        cpu_profiles: dict[str, CPUProfile] | None = None,
        skipped_rows: list[RunRow] | None = None,
    ) -> None:
        self._inner = inner
        self._mem = memory_profiles or {}
        self._cpu = cpu_profiles or {}
        # Pre-built skipped RunRows, flushed right after the context so they land
        # before `finalize` (where the Parquet reporter writes its file).
        self._skipped_rows = skipped_rows or []

    def report_context(self, context: dict[str, Any]) -> bool:
        ok = self._inner.report_context(context)
        if self._skipped_rows:
            self._inner.report_runs(self._skipped_rows)
        return ok

    def report_runs(self, runs: list[Run]) -> None:
        from mew.reporter import _run_to_dict

        rows: list[RunRow] = []
        for r in runs:
            row = _run_to_dict(r)
            # Key on structured name parts: benchmark_name()'s suffixes aren't in it.
            name = r.run_name
            key = _profile_key(name.function_name, name.args)
            if (mem := self._mem.get(key)) is not None:
                row["memory"] = dataclasses.asdict(mem)
            if (cpu := self._cpu.get(key)) is not None:
                row["cpu_profile"] = dataclasses.asdict(cpu)
            rows.append(row)
        self._inner.report_runs(rows)

    def finalize(self) -> None:
        if fn := getattr(self._inner, "finalize", None):
            fn()

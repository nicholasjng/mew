"""Shared infrastructure for profile-based extensions (memory, CPU, ...).

``_ProfileState`` runs a benchmark body outside Google Benchmark's iteration loop.
``EnrichedRun`` proxies a C++ Run while carrying optional profile attachments.
``_ProfileEnriching`` wraps a reporter to attach those profiles by benchmark name.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from mew._core import BenchmarkName, Run, RunType, TimeUnit
    from mew._registry import Entry
    from mew.cpu import CPUProfile
    from mew.memory import MemoryProfile


def _profile_key(function_name: str, args: str) -> str:
    """Profile lookup key from GB's *structured* name parts.

    Survives the ``/min_time:…``/aggregate suffixes on ``benchmark_name()``;
    yields ``entry.name/case:<i>`` for a family, ``entry.name`` otherwise.
    """
    return f"{function_name}/{args}" if args else function_name


def iter_entry_cases(entry: Entry) -> Iterator[tuple[str, int]]:
    """Yield ``(profile_key, range_value)`` per case an entry expands to.

    Families yield one pair per case so a profiler drives each variant via
    ``_ProfileState(range_value=i)`` under the key the reporter looks up.
    """
    if entry.case_labels is None:
        yield _profile_key(entry.name, ""), 0
    else:
        for i in range(len(entry.case_labels)):
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


@dataclass(frozen=True, slots=True)
class EnrichedRun:
    """Wraps a C++ Run and carries optional profile attachments.

    Run fields are forwarded explicitly so type checkers see the full public surface.
    """

    run: Run = field(repr=False)
    memory: MemoryProfile | None = None
    cpu: CPUProfile | None = None

    def benchmark_name(self) -> str:
        return self.run.benchmark_name()

    def adjusted_real_time(self) -> float:
        return self.run.adjusted_real_time()

    def adjusted_cpu_time(self) -> float:
        return self.run.adjusted_cpu_time()

    @property
    def run_name(self) -> BenchmarkName:
        return self.run.run_name

    @property
    def family_index(self) -> int:
        return self.run.family_index

    @property
    def per_family_instance_index(self) -> int:
        return self.run.per_family_instance_index

    @property
    def run_type(self) -> RunType:
        return self.run.run_type

    @property
    def aggregate_name(self) -> str:
        return self.run.aggregate_name

    @property
    def report_label(self) -> str:
        return self.run.report_label

    @property
    def skip_message(self) -> str:
        return self.run.skip_message

    @property
    def iterations(self) -> int:
        return self.run.iterations

    @property
    def threads(self) -> int:
        return self.run.threads

    @property
    def repetition_index(self) -> int:
        return self.run.repetition_index

    @property
    def repetitions(self) -> int:
        return self.run.repetitions

    @property
    def time_unit(self) -> TimeUnit:
        return self.run.time_unit

    @property
    def real_accumulated_time(self) -> float:
        return self.run.real_accumulated_time

    @property
    def cpu_accumulated_time(self) -> float:
        return self.run.cpu_accumulated_time

    @property
    def complexity_n(self) -> int:
        return self.run.complexity_n

    @property
    def counters(self) -> dict[str, float]:
        return self.run.counters

    @property
    def skipped(self) -> bool:
        return self.run.skipped


class _ProfileEnriching:
    """Reporter wrapper that attaches per-entry profiles to each Run by name."""

    def __init__(
        self,
        inner: Any,
        *,
        memory_profiles: dict[str, MemoryProfile] | None = None,
        cpu_profiles: dict[str, CPUProfile] | None = None,
    ) -> None:
        self._inner = inner
        self._mem = memory_profiles or {}
        self._cpu = cpu_profiles or {}

    def report_context(self, context: dict[str, Any]) -> bool:
        return self._inner.report_context(context)

    def report_runs(self, runs: list[Any]) -> None:
        enriched = []
        for r in runs:
            # Match structured parts, not benchmark_name(): its `/min_time:…`/aggregate
            # suffixes aren't in the key and would miss every parametrize case.
            name = r.run_name
            key = _profile_key(name.function_name, name.args)
            enriched.append(EnrichedRun(r, memory=self._mem.get(key), cpu=self._cpu.get(key)))
        self._inner.report_runs(enriched)

    def finalize(self) -> None:
        if fn := getattr(self._inner, "finalize", None):
            fn()

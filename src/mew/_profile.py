"""Shared infrastructure for profile-based extensions (memory, CPU, ...).

``_MockState`` runs a benchmark body outside Google Benchmark's iteration loop.
``EnrichedRun`` proxies a C++ Run while carrying optional profile attachments.
``_ProfileEnriching`` wraps a reporter to attach those profiles by benchmark name.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from mew._core import BenchmarkName, Run, RunType, TimeUnit
    from mew.cpu import CPUProfile
    from mew.memory import MemoryProfile


class _MockState:
    """Stand-in for ``_core.State`` that runs the loop body ``n_iterations`` times.

    Memory profiling needs one iteration; sampling CPU profilers need many to accumulate samples.
    """

    range_size: int = 0
    threads: int = 1
    thread_index: int = 0
    name: str = ""
    skipped: bool = False
    error_occurred: bool = False

    def __init__(self, n_iterations: int = 1) -> None:
        self._n = n_iterations
        self._i = 0

    @property
    def iterations(self) -> int:
        return self._n

    @property
    def max_iterations(self) -> int:
        return self._n

    def __iter__(self) -> _MockState:
        return self

    def __next__(self) -> None:
        if self._i >= self._n:
            raise StopIteration
        self._i += 1

    def keep_running_batch(self, n: int) -> bool:
        if self._i < self._n:
            self._i += n
            return True
        return False

    def batches(self, n: int) -> Iterator[int]:
        while self.keep_running_batch(n):
            yield n

    def pause(self) -> AbstractContextManager[None]:
        return nullcontext()

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
        return 0


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
        self._inner.report_runs(
            [
                EnrichedRun(
                    r,
                    memory=self._mem.get(r.benchmark_name()),
                    cpu=self._cpu.get(r.benchmark_name()),
                )
                for r in runs
            ]
        )

    def finalize(self) -> None:
        if fn := getattr(self._inner, "finalize", None):
            fn()

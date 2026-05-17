"""Shared infrastructure for profile-based extensions (memory, CPU, ...).

`_MockState` runs a benchmark body outside Google Benchmark's iteration loop.
`EnrichedRun` proxies a C++ Run while carrying optional profile attachments.
`_ProfileEnriching` wraps a reporter to attach those profiles by benchmark name.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from mew.cpu import CPUProfile
    from mew.memory import MemoryProfile


class _MockState:
    """Stand-in for _core.State that runs the loop body `n_iterations` times.

    Memory profiling needs one iteration (one allocation pattern); sampling
    CPU profilers need many to accumulate samples.
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

    def pause_timing(self) -> None:
        pass

    def resume_timing(self) -> None:
        pass

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


class EnrichedRun:
    """Wraps a C++ Run and carries optional profile attachments."""

    __slots__ = ("_run", "memory", "cpu")

    def __init__(
        self,
        run: Any,
        *,
        memory: MemoryProfile | None = None,
        cpu: CPUProfile | None = None,
    ) -> None:
        self._run = run
        self.memory = memory
        self.cpu = cpu

    def __getattr__(self, name: str) -> Any:
        return getattr(self._run, name)


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

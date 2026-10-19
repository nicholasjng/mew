"""Shared typing primitives for mew's public API."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from contextlib import AbstractContextManager
from typing import (
    Any,
    Literal,
    NotRequired,
    Protocol,
    TypeAlias,
    TypedDict,
    runtime_checkable,
)

from mew._core import CounterFlags, CounterOneK, TimeUnit

TimeUnitStr = Literal["ns", "us", "ms", "s"]


class SessionInfo(TypedDict):
    """Which run a result came from.

    ``mew compare`` reads a file's newest session unless ``path@<tag>`` names
    one, and pools sessions that share a ``tag`` as repetitions. ``mew_version``
    is the mew that wrote the rows, and so their format.
    """

    id: str
    date: str
    host: str
    mew_version: NotRequired[str]
    tag: NotRequired[str]


class BenchmarkResult(TypedDict):
    """One result row, as reporters receive it and result files store it.

    Every key is present except those marked optional.

    Attributes
    ----------
    benchmark : str
        The benchmark as mew addresses it: the registered name, ``[case label]``
        for a family case, ``/threads:N`` when the benchmark set ``threads``. Shared
        by all repetitions of one benchmark, unlike ``name``, which also carries
        Google Benchmark's option fragments.
    session : SessionInfo, optional
        Identity of the run that produced this result.
    context : dict, optional
        Provenance: :func:`mew.machine_context`, plus what was set with
        :func:`mew.set_context` or :func:`mew.update_context`.
    counters : list of dict
        The run's counters in name order, each with a ``name``, a ``value``
        (Google Benchmark has already applied the counter's flags) and, if the
        console shows it as more than a plain decimal number, a ``format``:
        ``one_k`` (1024 for binary prefixes), ``rate`` (shown per second),
        ``inverted`` (an inverted rate, shown in seconds) and ``exponent``
        (exponent notation).
    memory : dict, optional
        The memory manager's figures (``peak_bytes``, ``total_allocations``,
        ``iterations``, ``allocations_per_iteration``, and optionally
        ``total_bytes`` and ``net_heap_growth``), when one ran.
    cpu_profile : dict, optional
        The profiler manager's summary, when it returned one; a
        :class:`mew.cpu.CpuProfile` under ``--sample``.
    """

    name: str
    run_name: str
    benchmark: str
    family_index: int
    per_family_instance_index: int
    run_type: str
    aggregate_name: str
    repetitions: int
    repetition_index: int
    threads: int
    iterations: int
    real_time: float
    cpu_time: float
    real_accumulated_time: float
    cpu_accumulated_time: float
    time_unit: str
    label: str
    skipped: bool
    skip_message: str
    counters: list[dict[str, Any]]
    session: NotRequired[SessionInfo]
    context: NotRequired[dict[str, Any]]
    memory: NotRequired[dict[str, Any]]
    cpu_profile: NotRequired[dict[str, Any]]


class BenchmarkOptions(TypedDict, total=False):
    """Per-benchmark Google Benchmark options accepted by the decorators.

    All keys are optional; omit one to fall back to Google Benchmark's default.
    ``iterations`` fixes the iteration count, so it cannot be combined with
    ``min_time`` or ``min_warmup_time``.

    ``threads`` requires a free-threaded interpreter. On a GIL build,
    :func:`mew.run` skips threaded benchmarks unless ``strict=True``. An
    iterable runs the benchmark once per thread count, e.g. ``[1, 2, 4, 8]``.
    """

    min_time: float
    min_warmup_time: float
    iterations: int
    repetitions: int
    unit: TimeUnitStr | TimeUnit
    use_real_time: bool
    use_manual_time: bool
    measure_process_cpu_time: bool
    threads: int | Iterable[int]


class MemoryMetrics(TypedDict, total=False):
    """Measurements returned by a memory manager.

    Attributes
    ----------
    total_allocations : int, optional
        Number of allocations.
    peak_bytes : int, optional
        Maximum live bytes.
    total_bytes : int, optional
        Total allocated bytes.
    net_heap_growth : int, optional
        Live-byte increase over the measurement.
    """

    total_allocations: int
    peak_bytes: int
    total_bytes: int
    net_heap_growth: int


ProfilerSummary: TypeAlias = dict[str, Any]


@runtime_checkable
class MemoryManager(Protocol):
    """Memory-measurement callbacks accepted by :func:`mew.run`."""

    def start(self) -> None: ...
    def stop(self) -> MemoryMetrics | None: ...


@runtime_checkable
class ProfilerManager(Protocol):
    """Required profiler callbacks accepted by :func:`mew.run`."""

    def after_setup_start(self) -> None: ...
    def before_teardown_stop(self) -> None: ...


@runtime_checkable
class ProfilerResultProvider(Protocol):
    """Optional profiler capability that attaches a summary to each result."""

    def get_result(self) -> ProfilerSummary | None: ...


@runtime_checkable
class PausableProfiler(Protocol):
    """Optional profiler capability that suspends across ``state.pause()``.

    Calls come from the profiling thread only. A ``pause()`` may go unmatched
    when a body leaves a pause scope open at the end of its loop; the next
    ``before_teardown_stop()`` then arrives while paused.
    """

    def pause(self) -> None: ...
    def resume(self) -> None: ...


@runtime_checkable
class State(Protocol):
    """What a benchmark function receives first: iterate it to run the timed loop."""

    range_size: int
    threads: int
    thread_index: int
    name: str
    skipped: bool
    error_occurred: bool

    @property
    def iterations(self) -> int: ...
    @property
    def max_iterations(self) -> int: ...
    def __iter__(self) -> State: ...
    def __next__(self) -> None: ...
    def batches(self, n: int) -> Iterator[int]: ...
    def pause(self) -> AbstractContextManager[None]: ...
    def set_counter(
        self,
        name: str,
        value: float,
        flags: CounterFlags = ...,
        one_k: CounterOneK = ...,
    ) -> None: ...
    def set_label(self, label: str) -> None: ...
    def set_items_processed(self, n: int) -> None: ...
    def set_bytes_processed(self, n: int) -> None: ...
    def set_iteration_time(self, seconds: float) -> None: ...
    def skip_with_error(self, msg: str) -> None: ...
    def skip_with_message(self, msg: str) -> None: ...
    def range(self, pos: int = 0) -> int: ...


@runtime_checkable
class BenchmarkFn(Protocol):
    """A benchmark function: a :class:`State` first, then a family case's parameters
    as keyword arguments."""

    __name__: str
    __qualname__: str

    @property
    def __globals__(self) -> dict[str, Any]: ...

    def __call__(self, state: State, /, *args: Any, **kwargs: Any) -> None: ...

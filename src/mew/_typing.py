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
        ``total_bytes`` and ``net_heap_growth``), when one ran and measured
        something.
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
    ``min_time`` takes seconds (``0.5``, ``"0.5s"``, ``"500ms"``, ``"1m"``) or,
    as for :func:`mew.run`, a fixed iteration count (``"100x"``), the same as
    ``iterations=100``. A fixed iteration count cannot be combined with
    ``min_time`` in seconds or ``min_warmup_time``. ``use_real_time`` cannot be
    combined with ``use_manual_time``.

    ``threads`` requires a free-threaded interpreter. On a GIL build,
    :func:`mew.run` skips threaded benchmarks unless ``strict=True``. An
    iterable runs the benchmark once per thread count, e.g. ``[1, 2, 4, 8]``.
    """

    min_time: float | str
    min_warmup_time: float
    iterations: int
    repetitions: int
    unit: TimeUnitStr | TimeUnit
    use_real_time: bool
    use_manual_time: bool
    measure_process_cpu_time: bool
    threads: int | Iterable[int]


class MemoryMetrics(TypedDict):
    """Measurements returned by a memory manager's ``stop()``.

    Attributes
    ----------
    peak_bytes : int
        Maximum live bytes.
    total_allocations : int
        Number of allocations.
    total_bytes : int, optional
        Total allocated bytes.
    net_heap_growth : int, optional
        Live-byte increase over the measurement.
    """

    peak_bytes: int
    total_allocations: int
    total_bytes: NotRequired[int]
    net_heap_growth: NotRequired[int]


ProfilerSummary: TypeAlias = dict[str, Any]


@runtime_checkable
class MemoryManager(Protocol):
    """Memory-measurement callbacks accepted by :func:`mew.run`.

    ``start()`` runs at the first step of the memory pass's timing loop and
    ``stop()`` after its last. ``stop()`` returns :class:`MemoryMetrics`, a
    dict of ints with ``peak_bytes`` and ``total_allocations`` and optionally
    ``total_bytes`` and ``net_heap_growth``, or ``None`` when it measured
    nothing, which leaves the row without a ``memory`` block. A missing
    required key, an unknown key or a value that isn't an int fails the run
    with ``TypeError``.
    """

    def start(self) -> None: ...
    def stop(self) -> MemoryMetrics | None: ...


@runtime_checkable
class ProfilerManager(Protocol):
    """Required profiler callbacks accepted by :func:`mew.run`."""

    def after_setup_start(self) -> None: ...
    def before_teardown_stop(self) -> None: ...


@runtime_checkable
class ProfilerResultProvider(Protocol):
    """Optional profiler capability that attaches a summary to each result.

    ``get_result()`` runs after each profiling pass. A dict, even an empty one,
    becomes the row's ``cpu_profile`` block; ``None`` leaves the block out.
    """

    def get_result(self) -> ProfilerSummary | None: ...


@runtime_checkable
class PausableProfiler(Protocol):
    """Optional profiler capability that suspends across ``state.pause()``.

    Calls come from the profiling thread only, and in pairs: every ``pause()``
    is followed by a ``resume()`` before ``before_teardown_stop()``. When a
    body leaves a pause scope open at the end of its loop, mew calls
    ``resume()`` itself before ``before_teardown_stop()``.
    """

    def pause(self) -> None: ...
    def resume(self) -> None: ...


@runtime_checkable
class State(Protocol):
    """What a benchmark function receives first: iterate it to run the timed loop."""

    # Keep the docstrings in sync with src/_mew/state.cpp.
    @property
    def range_size(self) -> int:
        """Number of range arguments available to :meth:`range`."""
        ...

    @property
    def threads(self) -> int:
        """Threads in this run; 1 without threaded mode or in a profiling pass."""
        ...

    @property
    def thread_index(self) -> int:
        """Index of the thread owning this State, in ``[0, threads)``."""
        ...

    @property
    def name(self) -> str:
        """The registered benchmark name."""
        ...

    @property
    def skipped(self) -> bool:
        """Whether this benchmark was skipped, by either ``skip_with_*`` call."""
        ...

    @property
    def error_occurred(self) -> bool:
        """Whether the skip came from :meth:`skip_with_error` rather than
        :meth:`skip_with_message`."""
        ...

    @property
    def iterations(self) -> int:
        """Iterations completed so far; the total once the loop finishes."""
        ...

    @property
    def max_iterations(self) -> int:
        """Iteration count Google Benchmark budgeted for this run."""
        ...

    def __iter__(self) -> State: ...
    def __next__(self) -> None: ...

    def batches(self, n: int) -> Iterator[int]:
        """Iterate in batches of ``n``, reducing dispatch overhead for fast bodies.

        Each step yields ``n``; the final batch may exceed the iteration budget.
        """
        ...

    def pause(self) -> AbstractContextManager[object]:
        """Return a context manager that pauses timing for its ``with`` block.

        Inside another pause it does nothing. Entering it outside the benchmark
        loop, or again while it is active, raises :class:`RuntimeError`.
        """
        ...

    def set_counter(
        self,
        name: str,
        value: float,
        flags: CounterFlags = CounterFlags.kDefaults,
        one_k: CounterOneK = CounterOneK.kIs1000,
    ) -> None:
        """Attach a user-defined counter, surfaced in the row's ``counters``.

        ``flags`` controls normalization (and rate/exponent display); ``one_k``
        selects decimal or binary prefixes when the console shows the counter.
        """
        ...

    def set_label(self, label: str) -> None:
        """Attach a free-form label to this benchmark's reported row."""
        ...

    def set_items_processed(self, items: int) -> None:
        """Record how many items the body handled, reported as items/second."""
        ...

    def set_bytes_processed(self, n_bytes: int) -> None:
        """Record how many bytes the body handled, reported as bytes/second."""
        ...

    def set_iteration_time(self, seconds: float) -> None:
        """Report the elapsed time of one iteration yourself.

        Only honored when the benchmark sets ``use_manual_time``.
        """
        ...

    def skip_with_error(self, msg: str) -> None:
        """Abort this benchmark and mark it failed; the row reports as skipped."""
        ...

    def skip_with_message(self, msg: str) -> None:
        """Abort this benchmark without marking it an error (e.g. an unmet precondition)."""
        ...

    def range(self, pos: int = 0) -> int:
        """Return the range argument at ``pos``."""
        ...


@runtime_checkable
class BenchmarkFn(Protocol):
    """A benchmark function: a :class:`State` first, then a family case's parameters
    as keyword arguments."""

    __name__: str
    __qualname__: str

    def __call__(self, state: State, /, *args: Any, **kwargs: Any) -> None: ...

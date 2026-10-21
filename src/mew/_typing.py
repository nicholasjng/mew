"""Types and protocols of mew's public API."""

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
    """The identity of the run a result came from.

    Attributes
    ----------
    id : str
        A time-ordered UUIDv7, unique to the run.
    date : str
        When the run started, in ISO 8601.
    host : str
        The machine's host name.
    mew_version : str, optional
        The mew that wrote the row, and so its format.
    tag : str, optional
        The run's session tag.
    """

    id: str
    date: str
    host: str
    mew_version: NotRequired[str]
    tag: NotRequired[str]


class BenchmarkResult(TypedDict):
    """The result of one benchmark repetition: its timings, counters and provenance.

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
    """Per-benchmark Google Benchmark options.

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
    """The memory figures of one profiling pass.

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
    """Protocol for measuring the memory a benchmark body allocates.

    :func:`mew.run` calls ``start()`` and ``stop()`` around the timing loop of an
    extra, untimed pass per benchmark and repetition, and stores the figures in
    the row's ``memory`` block.
    """

    def start(self) -> None:
        """Start measuring.

        Called at the first step of the memory pass's timing loop.
        """
        ...

    def stop(self) -> MemoryMetrics | None:
        """Stop measuring and return the figures.

        Called after the pass's last loop step. A dict with a missing required
        key, an unknown key or a value that isn't an int fails the run with
        ``TypeError``.

        Returns
        -------
        MemoryMetrics or None
            The figures, or ``None`` when nothing was measured, which leaves the
            row without a ``memory`` block.
        """
        ...


@runtime_checkable
class ProfilerManager(Protocol):
    """Protocol for profiling a benchmark body.

    :func:`mew.run` starts and stops the profiler around the timing loop of an
    extra, untimed pass per benchmark and repetition. A profiler may also
    implement :class:`ProfilerResultProvider` and :class:`PausableProfiler`.
    """

    def after_setup_start(self) -> None:
        """Start profiling.

        Called at the first step of the profiling pass's timing loop.
        """
        ...

    def before_teardown_stop(self) -> None:
        """Stop profiling.

        Called after the pass's last loop step.
        """
        ...


@runtime_checkable
class ProfilerResultProvider(Protocol):
    """Protocol for profilers that summarize each profiling pass.

    ``get_result()`` is called after each pass, and its summary stored in the
    row's ``cpu_profile`` block.
    """

    def get_result(self) -> ProfilerSummary | None:
        """Summarize the pass that just ended.

        Returns
        -------
        ProfilerSummary or None
            The summary, an empty dict included, or ``None`` to leave the row
            without a ``cpu_profile`` block.
        """
        ...


@runtime_checkable
class PausableProfiler(Protocol):
    """Protocol for profilers that pause during ``state.pause()`` scopes.

    Calls come from the profiling thread only, and in pairs: every ``pause()``
    is followed by a ``resume()`` before ``before_teardown_stop()``. When a
    body leaves a pause scope open at the end of its loop, mew calls
    ``resume()`` itself before ``before_teardown_stop()``.
    """

    def pause(self) -> None:
        """Suspend profiling.

        Called when the body enters a ``state.pause()`` scope.
        """
        ...

    def resume(self) -> None:
        """Resume profiling.

        Called when the pause scope exits.
        """
        ...


@runtime_checkable
class State(Protocol):
    """The state of a running benchmark; iterating it runs the timed loop.

    Only the body of ``for _ in state:`` is timed; code before and after the
    loop is setup and teardown.
    """

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
        """Whether :meth:`skip_with_error`, not :meth:`skip_with_message`, skipped it."""
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

        Parameters
        ----------
        n : int
            Iterations per batch.

        Yields
        ------
        int
            ``n`` at each step; the final batch may exceed the iteration budget.

        Raises
        ------
        ValueError
            If ``n`` is less than 1.
        """
        ...

    def pause(self) -> AbstractContextManager[object]:
        """Return a context manager that pauses timing for its ``with`` block.

        Inside another pause it does nothing. Entering it outside the benchmark
        loop, or again while it is active, raises :class:`RuntimeError`.

        Returns
        -------
        AbstractContextManager
            The pause scope.
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

        Parameters
        ----------
        name : str
            The counter's name.
        value : float
            Its value, before ``flags`` apply.
        flags : CounterFlags, default CounterFlags.kDefaults
            How Google Benchmark normalizes the value, e.g. as a rate, and
            whether the console shows it in exponent notation.
        one_k : CounterOneK, default CounterOneK.kIs1000
            Whether the console scales the value with decimal or binary prefixes.
        """
        ...

    def set_label(self, label: str) -> None:
        """Attach a free-form label to this benchmark's reported row.

        Parameters
        ----------
        label : str
            The label; it replaces one set before.
        """
        ...

    def set_items_processed(self, items: int) -> None:
        """Record how many items the body handled, reported as items/second.

        Parameters
        ----------
        items : int
            Items handled over all iterations.
        """
        ...

    def set_bytes_processed(self, n_bytes: int) -> None:
        """Record how many bytes the body handled, reported as bytes/second.

        Parameters
        ----------
        n_bytes : int
            Bytes handled over all iterations.
        """
        ...

    def set_iteration_time(self, seconds: float) -> None:
        """Report the elapsed time of one iteration yourself.

        Only honored when the benchmark sets ``use_manual_time``.

        Parameters
        ----------
        seconds : float
            The iteration's elapsed time in seconds.
        """
        ...

    def skip_with_error(self, msg: str) -> None:
        """Abort this benchmark and mark it failed; the row reports as skipped.

        Parameters
        ----------
        msg : str
            The reason, reported as the row's ``skip_message``.
        """
        ...

    def skip_with_message(self, msg: str) -> None:
        """Abort this benchmark without marking it an error (e.g. an unmet precondition).

        Parameters
        ----------
        msg : str
            The reason, reported as the row's ``skip_message``.
        """
        ...

    def range(self, pos: int = 0) -> int:
        """Return a range argument.

        Parameters
        ----------
        pos : int, default 0
            The argument's index, below :attr:`range_size`.

        Returns
        -------
        int
            The argument at ``pos``.

        Raises
        ------
        IndexError
            If ``pos`` is out of bounds.
        """
        ...


@runtime_checkable
class BenchmarkFn(Protocol):
    """A benchmark function, called with a :class:`State` and its case's parameters.

    A case of a family from :func:`mew.parametrize` or :func:`mew.product` passes
    its parameters as keyword arguments; a plain benchmark gets none.
    """

    __name__: str
    __qualname__: str

    def __call__(self, state: State, /, *args: Any, **kwargs: Any) -> None: ...

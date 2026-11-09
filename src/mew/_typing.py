"""Types and protocols of mew's public API."""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from contextlib import AbstractContextManager
from pathlib import Path
from typing import (
    Any,
    Literal,
    NotRequired,
    Protocol,
    TypeAlias,
    TypedDict,
    runtime_checkable,
)

from mew._core import CounterFlags, CounterOneK


class RunInfo(TypedDict):
    """The identity of the run a result came from: one call of :func:`mew.run`.

    Attributes
    ----------
    id : str
        A time-ordered UUIDv7, unique to the run.
    date : str
        When the run started, in UTC with milliseconds, e.g.
        ``"2026-11-08T12:24:23.559Z"``.
    name : str, optional
        The run's name, from ``--run-name`` or ``run_name=``; ``path@<name>``
        selects the newest run of that name.
    """

    id: str
    date: str
    name: NotRequired[str]


class SkipInfo(TypedDict):
    """Why a benchmark's row has no measurement.

    Attributes
    ----------
    reason : {"error", "skipped"}
        ``"error"`` for ``state.skip_with_error()`` and a benchmark that raised,
        ``"skipped"`` for ``state.skip_with_message()``.
    message : str
        The message, or ``Type: message`` for a benchmark that raised.
    """

    reason: Literal["error", "skipped"]
    message: str


class BenchmarkResult(TypedDict):
    """The result of one benchmark repetition: its timings, counters and provenance.

    Every key is present except those marked optional. Result files add
    ``schema_version`` to each row.

    Attributes
    ----------
    name : str
        The benchmark as mew addresses it: the registered name, ``[case label]``
        for a family case, ``/threads:N`` when the benchmark set ``threads``.
        Shared by all repetitions of one benchmark.
    params : dict
        A family case's parameters, ``{}`` for a plain benchmark. Values JSON
        represents as themselves are kept; any other is its type's qualified
        name, such as ``"torch.Tensor"``.
    repetition, repetitions : int
        Which repetition this is, from 0, of how many.
    threads : int
        Threads the benchmark ran on.
    iterations : int
        Iterations of the timed loop.
    real_time, cpu_time : float
        Per-iteration times in nanoseconds.
    label : str
        A label the benchmark set with ``state.set_label()``.
    skip : SkipInfo or None
        Why the row has no measurement, or ``None`` for a measured row.
    counters : list of dict
        The run's counters in name order, each with a ``name``, a ``value``
        (Google Benchmark has already applied the counter's flags) and, if the
        console shows it as more than a plain decimal number, a ``format``:
        ``one_k`` (1024 for binary prefixes), ``rate`` (shown per second),
        ``inverted`` (an inverted rate, shown in seconds) and ``exponent``
        (exponent notation).
    run : RunInfo, optional
        Identity of the run that produced this result.
    context : dict, optional
        Provenance: the mew version as ``mew``, :func:`mew.machine_context`,
        and what the run's ``context=``, or a setup file's ``context()`` hook,
        added.
    memory : dict, optional
        The memory manager's figures (``peak_bytes``, ``total_allocations``,
        ``iterations``, ``allocations_per_iteration``, and optionally
        ``total_bytes`` and ``net_heap_growth``), when one ran and measured
        something.
    cpu_profile : dict, optional
        The profiler manager's summary, when it returned one; a
        :class:`mew.cpu.CpuProfile` under ``--cpu-profiler sample``.
    """

    name: str
    params: dict[str, Any]
    repetition: int
    repetitions: int
    threads: int
    iterations: int
    real_time: float
    cpu_time: float
    label: str
    skip: SkipInfo | None
    counters: list[dict[str, Any]]
    schema_version: NotRequired[int]
    run: NotRequired[RunInfo]
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


# Mapping, so a TypedDict such as mew.cpu.CpuProfile matches; at runtime, a dict.
ProfilerSummary: TypeAlias = Mapping[str, Any]


@runtime_checkable
class MemoryManager(Protocol):
    """Protocol for measuring the memory a benchmark body allocates.

    :func:`mew.run` calls ``start()`` and ``stop()`` around the timing loop of an
    extra, untimed pass per benchmark and repetition, and stores the figures in
    the row's ``memory`` block. A subclass may override :meth:`write_outputs`
    to write files after the run.
    """

    def start(self) -> None:
        """Start measuring.

        Called at the first step of the memory pass's timing loop.
        """
        ...

    def stop(self) -> MemoryMetrics | None:
        """Stop measuring and return the figures.

        Called after the pass's last loop step. A dict with a missing required
        key or a value that isn't an int fails the run with ``TypeError``;
        other keys are ignored.

        Returns
        -------
        MemoryMetrics or None
            The figures, or ``None`` when nothing was measured, which leaves the
            row without a ``memory`` block.
        """
        ...

    def write_outputs(self, directory: Path) -> None:
        """Write the manager's files, such as an HTML report, to ``directory``.

        Called after the run by ``mew run --profile-dir DIR``, with
        ``DIR/memory``. Only for files: the row's figures come from
        :meth:`stop`.
        Does nothing unless a subclass overrides it.

        Parameters
        ----------
        directory : Path
            Where to write; it may not exist yet.
        """
        return


@runtime_checkable
class ProfilerManager(Protocol):
    """Protocol for profiling a benchmark body.

    :func:`mew.run` starts and stops the profiler around the timing loop of an
    extra, untimed pass per benchmark and repetition. Beyond starting and
    stopping, each hook does nothing unless a subclass overrides it: a
    profiler that summarizes its passes overrides :meth:`get_result`, one that
    can pause overrides :meth:`pause` and :meth:`resume`, and one that writes
    files overrides :meth:`write_outputs`.
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

    def get_result(self) -> ProfilerSummary | None:
        """Summarize the pass that just ended.

        Called after each pass; the summary goes into the row's ``cpu_profile``
        block. Returns ``None`` unless a subclass overrides it.

        Returns
        -------
        ProfilerSummary or None
            The summary, an empty dict included, or ``None`` to leave the row
            without a ``cpu_profile`` block.
        """
        return None

    def pause(self) -> None:
        """Suspend profiling during a ``state.pause()`` scope.

        Calls come from the profiling thread only, and in pairs: every
        ``pause()`` is followed by a ``resume()`` before
        ``before_teardown_stop()``. Does nothing unless a subclass overrides
        it, so the paused region is profiled.
        """
        return

    def resume(self) -> None:
        """Resume profiling when a ``state.pause()`` scope exits.

        Does nothing unless a subclass overrides it.
        """
        return

    def write_outputs(self, directory: Path) -> None:
        """Write the manager's files, such as an HTML report, to ``directory``.

        Called after the run by ``mew run --profile-dir DIR``, with
        ``DIR/cpu``. Only for files: the row's figures come from
        :meth:`get_result`. Does nothing unless a subclass overrides it.

        Parameters
        ----------
        directory : Path
            Where to write; it may not exist yet.
        """
        return


@runtime_checkable
class State(Protocol):
    """The state of a running benchmark; iterating it runs the timed loop.

    Only the body of ``for _ in state:`` is timed; code before and after the
    loop is setup and teardown.
    """

    # Keep the docstrings in sync with src/_mew/state.cpp.
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
            The reason, reported as the row's ``skip.message``.
        """
        ...

    def skip_with_message(self, msg: str) -> None:
        """Abort this benchmark without marking it an error (e.g. an unmet precondition).

        Parameters
        ----------
        msg : str
            The reason, reported as the row's ``skip.message``.
        """
        ...


@runtime_checkable
class BenchmarkFn(Protocol):
    """A benchmark function, called with a :class:`State` and its case's parameters.

    A case of a family from :func:`mew.parametrize` passes
    its parameters as keyword arguments; a plain benchmark gets none.
    """

    __name__: str
    __qualname__: str

    def __call__(self, state: State, /, *args: Any, **kwargs: Any) -> None: ...

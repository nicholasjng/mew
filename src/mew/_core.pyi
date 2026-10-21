"""The mew C++ core (Google Benchmark bindings)."""

import enum
import types
from collections.abc import Callable, Sequence
from typing import Self

BENCHMARK_COMMIT: str = "00aed1d2cc650262186fa35338a178425270415a"

BENCHMARK_VERSION: str = "v1.10.0-4-g00aed1d2+mew"

def warmup_free_threading() -> None:
    """Attach one native thread to initialize free-threaded CPython state."""

def cpu_info() -> dict:
    """
    CPU count, and whether frequency scaling was detected (sysfs on Linux, sysctl on macOS).
    """

class TimeUnit(enum.StrEnum):
    """A time unit for per-iteration durations."""

    ns = "ns"

    us = "us"

    ms = "ms"

    s = "s"

def run_benchmarks(
    argv: Sequence[str],
    reporter: object,
    session_context: dict = {},
    skipped_rows: list = [],
    capture: object | None = None,
) -> int:
    """
    Initialize Google Benchmark with `argv` and run all registered benchmarks,
    reporting through `reporter`.
    `capture` (an active mew._capture.OutputCapture) is suspended around every
    reporter callback, so reporters write to the real stdout/stderr.
    Returns the number of benchmarks run.
    """

class CounterFlags(enum.IntFlag):
    """
    Flags that set how Google Benchmark normalizes and shows a counter.

    Combine them with ``|``, e.g. ``kIsRate | kInvert``.
    """

    def __repr__(self, /):
        """Return repr(self)."""

    kDefaults = 0

    kIsRate = 1

    kAvgThreads = 2

    kAvgThreadsRate = 3

    kIsIterationInvariant = 4

    kIsIterationInvariantRate = 5

    kAvgIterations = 8

    kAvgIterationsRate = 9

    kStringifyExponent = 16

    kInvert = -2147483648

class CounterOneK(enum.Enum):
    """The base, 1000 or 1024, of a counter's displayed prefixes."""

    kIs1000 = 1000

    kIs1024 = 1024

class BatchIter:
    """Iterator yielding batch sizes from `State.batches`."""

    def __iter__(self) -> BatchIter: ...
    def __next__(self) -> int: ...

class PauseScope:
    """Context manager that pauses State timing within a scope."""

    def __enter__(self) -> Self: ...
    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: types.TracebackType | None,
    ) -> None: ...

class State:
    """
    The state of a running benchmark; iterating it runs the timed loop.

    Only the body of ``for _ in state:`` is timed; code before and after the
    loop is setup and teardown.
    """

    def __iter__(self) -> State: ...
    def __next__(self) -> None: ...
    def pause(self) -> PauseScope:
        """
        Return a context manager that pauses timing for its ``with`` block.

        Inside another pause it does nothing. Entering it outside the benchmark
        loop, or again while it is active, raises :class:`RuntimeError`.

        Returns
        -------
        AbstractContextManager
            The pause scope.
        """

    def batches(self, n: int) -> BatchIter:
        """
        Iterate in batches of ``n``, reducing dispatch overhead for fast bodies.

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

    def skip_with_error(self, msg: str) -> None:
        """
        Abort this benchmark and mark it failed; the row reports as skipped.

        Parameters
        ----------
        msg : str
            The reason, reported as the row's ``skip_message``.
        """

    def skip_with_message(self, msg: str) -> None:
        """
        Abort this benchmark without marking it an error (e.g. an unmet precondition).

        Parameters
        ----------
        msg : str
            The reason, reported as the row's ``skip_message``.
        """

    def set_label(self, label: str) -> None:
        """
        Attach a free-form label to this benchmark's reported row.

        Parameters
        ----------
        label : str
            The label; it replaces one set before.
        """

    def set_iteration_time(self, seconds: float) -> None:
        """
        Report the elapsed time of one iteration yourself.

        Only honored when the benchmark sets ``use_manual_time``.

        Parameters
        ----------
        seconds : float
            The iteration's elapsed time in seconds.
        """

    def set_items_processed(self, items: int) -> None:
        """
        Record how many items the body handled, reported as items/second.

        Parameters
        ----------
        items : int
            Items handled over all iterations.
        """

    def set_bytes_processed(self, n_bytes: int) -> None:
        """
        Record how many bytes the body handled, reported as bytes/second.

        Parameters
        ----------
        n_bytes : int
            Bytes handled over all iterations.
        """

    def set_counter(
        self,
        name: str,
        value: float,
        flags: CounterFlags = CounterFlags.kDefaults,
        one_k: CounterOneK = CounterOneK.kIs1000,
    ) -> None:
        """
        Attach a user-defined counter, surfaced in the row's ``counters``.

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

    def range(self, pos: int = 0) -> int:
        """
        Return a range argument.

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

    @property
    def range_size(self) -> int:
        """Number of range arguments available to :meth:`range`."""

    @property
    def iterations(self) -> int:
        """Iterations completed so far; the total once the loop finishes."""

    @property
    def threads(self) -> int:
        """Threads in this run; 1 without threaded mode or in a profiling pass."""

    @property
    def thread_index(self) -> int:
        """Index of the thread owning this State, in ``[0, threads)``."""

    @property
    def name(self) -> str:
        """The registered benchmark name."""

    @property
    def skipped(self) -> bool:
        """Whether this benchmark was skipped, by either ``skip_with_*`` call."""

    @property
    def error_occurred(self) -> bool:
        """
        Whether :meth:`skip_with_error`, not :meth:`skip_with_message`, skipped it.
        """

    @property
    def max_iterations(self) -> int:
        """Iteration count Google Benchmark budgeted for this run."""

class BenchmarkHandle:
    """
    Handle to a registered Google Benchmark.
    Invalidated by the next `clear_registered_benchmarks()` call or interpreter shutdown; using a stale handle is undefined behaviour.
    """

    def min_time(self, seconds: float) -> BenchmarkHandle:
        """Run at least this many seconds before reporting."""

    def min_warmup_time(self, seconds: float) -> BenchmarkHandle:
        """Warm up for this many seconds before measuring."""

    def iterations(self, n: int) -> BenchmarkHandle:
        """Run exactly `n` iterations instead of timing out on `min_time`."""

    def repetitions(self, n: int) -> BenchmarkHandle:
        """
        Repeat the whole benchmark `n` times; variance metrics need at least 2.
        """

    def unit(self, unit: TimeUnit) -> BenchmarkHandle:
        """Time unit for the reported per-iteration durations."""

    def use_real_time(self) -> BenchmarkHandle:
        """Report wall-clock rather than CPU time as the primary measure."""

    def use_manual_time(self) -> BenchmarkHandle:
        """
        Take timings from `State.set_iteration_time` instead of the built-in timer.
        """

    def measure_process_cpu_time(self) -> BenchmarkHandle:
        """
        Measure CPU time across the whole process, not just the running thread.
        """

    def threads(self, n: int) -> BenchmarkHandle:
        """
        Run the benchmark with `n` threads, each with its own State and timer.
        Repeat to run once per thread count. Requires a free-threaded interpreter; mew skips it otherwise.
        """

    def arg(self, value: int) -> BenchmarkHandle:
        """
        Register one case with a single range argument, readable via `State.range`.
        """

    def arg_name(self, name: str) -> BenchmarkHandle:
        """Name the first range argument; appears in the reported benchmark name."""

def register_benchmark(name: str, fn: Callable) -> BenchmarkHandle:
    """
    Register `fn` as a benchmark under `name` and return a chainable handle.
    """

def clear_registered_benchmarks() -> None:
    """Drop all previously registered benchmarks from the global registry."""

def register_memory_manager(manager: object) -> None:
    """
    Register `manager` as Google Benchmark's memory manager, replacing any other.
    Requires `start()` and `stop()`; `stop()` returns memory metrics or None.
    Pair with `unregister_memory_manager`.
    """

def unregister_memory_manager() -> None: ...
def register_profiler_manager(manager: object) -> None:
    """
    Register `manager` as Google Benchmark's profiler manager, replacing any other.
    Requires `after_setup_start()` and `before_teardown_stop()`; supports optional
    `get_result()`, `pause()`, and `resume()` hooks.
    Pair with `unregister_profiler_manager`.
    """

def unregister_profiler_manager() -> None: ...

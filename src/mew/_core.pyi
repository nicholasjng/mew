"""The mew C++ core (Google Benchmark bindings)."""

import enum
import types
from collections.abc import Callable, Sequence
from typing import Self

BENCHMARK_COMMIT: str = "a8460680f0df91fd26205e0931708a26c3b4094d"

BENCHMARK_VERSION: str = "v1.9.5-74-ga8460680-dirty"

def warmup_free_threading() -> None:
    """Attach one native thread to initialize free-threaded CPython state."""

def preload_system_info() -> None:
    """Initialize Google Benchmark's CPU and system information."""

def cpu_info() -> dict:
    """
    CPU count and frequency-scaling state.
    Scaling probes sysfs on Linux and sysctl on macOS. `"unknown"` when undetectable.
    """

class TimeUnit(enum.StrEnum):
    """Time unit used for reported per-iteration durations."""

    ns = "ns"

    us = "us"

    ms = "ms"

    s = "s"

def run_benchmarks(
    argv: Sequence[str],
    reporter: object | None = None,
    extra_context: dict = {},
    extra_rows: list = [],
) -> int:
    """
    Initialize Google Benchmark with `argv` and run all registered benchmarks.
    Returns the number of benchmarks run.
    """

class CounterFlags(enum.IntFlag):
    """
    Flags forwarded to `benchmark::Counter`.
    OR together to combine (e.g. `kIsRate | kInvert`).
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

    kInvert = -2147483648

class CounterOneK(enum.Enum):
    """Base used to scale a counter for display."""

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
    Active microbenchmark state.
    Iterate with `for _ in state:` to time the body.
    """

    def __iter__(self) -> State: ...
    def __next__(self) -> None: ...
    def keep_running_batch(self, n: int) -> bool:
        """Advance by `n` iterations and return whether another batch should run."""

    def batches(self, n: int) -> BatchIter:
        """
        Iterate in batches of `n`, reducing dispatch overhead for fast bodies.
        The final batch may exceed the iteration budget.
        """

    def pause(self) -> PauseScope:
        """
        Return a context manager that pauses timing for the duration of the `with` block.
        """

    def skip_with_error(self, msg: str) -> None:
        """Abort this benchmark and mark it failed; the row reports as skipped."""

    def skip_with_message(self, msg: str) -> None:
        """
        Abort this benchmark without marking it an error (e.g. an unmet precondition).
        """

    def set_label(self, label: str) -> None:
        """Attach a free-form label to this benchmark's reported row."""

    def set_iteration_time(self, seconds: float) -> None:
        """
        Report the elapsed time of one iteration yourself. Only honored when the benchmark sets `use_manual_time`.
        """

    def set_items_processed(self, items: int) -> None:
        """Record how many items the body handled, reported as items/second."""

    def set_bytes_processed(self, n_bytes: int) -> None:
        """Record how many bytes the body handled, reported as bytes/second."""

    def set_counter(
        self,
        name: str,
        value: float,
        flags: CounterFlags = CounterFlags.kDefaults,
        one_k: CounterOneK = CounterOneK.kIs1000,
    ) -> None:
        """
        Attach a user-defined counter, surfaced in `BenchmarkResult['counters']`.
        `flags` controls normalization; `one_k` selects decimal or binary scaling.
        """

    def range(self, pos: int = 0) -> int:
        """Return the range argument at `pos`."""

    @property
    def range_size(self) -> int:
        """Number of range arguments available to `range`."""

    @property
    def iterations(self) -> int:
        """Iterations completed so far; the total once the loop finishes."""

    @property
    def threads(self) -> int:
        """Total number of threads in this run (1 unless threaded mode is on)."""

    @property
    def thread_index(self) -> int:
        """Index of the thread owning this State, in `[0, threads)`."""

    @property
    def name(self) -> str:
        """The registered benchmark name."""

    @property
    def skipped(self) -> bool:
        """Whether this benchmark was skipped, by either skip_with_* call."""

    @property
    def error_occurred(self) -> bool:
        """
        Whether the skip came from `skip_with_error` rather than `skip_with_message`.
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

    def report_aggregates_only(self, value: bool = True) -> BenchmarkHandle:
        """Emit only the aggregate rows, suppressing per-repetition ones."""

    def dense_range(self, start: int, limit: int, step: int = 1) -> BenchmarkHandle:
        """
        Register one case per value in `[start, limit]`, readable via `State.range`.
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

    @property
    def name(self) -> str:
        """The registered benchmark name."""

def register_benchmark(name: str, fn: Callable) -> BenchmarkHandle:
    """
    Register `fn` as a benchmark under `name` and return a chainable handle.
    """

def clear_registered_benchmarks() -> None:
    """Drop all previously registered benchmarks from the global registry."""

def register_memory_manager(manager: object) -> None:
    """
    Register `manager` as Google Benchmark's memory manager.
    Requires `start()` and `stop()`; `stop()` returns memory metrics or None.
    Pair with `unregister_memory_manager`.
    """

def unregister_memory_manager() -> None: ...
def register_profiler_manager(manager: object) -> None:
    """
    Register `manager` as Google Benchmark's profiler manager.
    Requires `after_setup_start()` and `before_teardown_stop()`; supports optional
    `get_result()`, `pause()`, and `resume()` hooks.
    Pair with `unregister_profiler_manager`.
    """

def unregister_profiler_manager() -> None: ...

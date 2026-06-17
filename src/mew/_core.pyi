"""The mew C++ core (Google Benchmark bindings)."""

import enum
import types
from collections.abc import Callable, Sequence
from typing import Self

BENCHMARK_COMMIT: str = "a8460680f0df91fd26205e0931708a26c3b4094d"

BENCHMARK_VERSION: str = "v1.9.5-74-ga8460680"

def preload_system_info() -> None:
    """
    Force Google Benchmark's lazy CPU/system-info probes to run now.
    Their platform diagnostics go straight to fd 2 (e.g. the macOS
    hw.cpufrequency sysctl failure); calling this under a scoped fd-2
    redirect keeps that noise out of user-visible stderr without
    silencing the benchmark run itself.
    """

class TimeUnit(enum.StrEnum):
    """Time unit used for reported per-iteration durations."""

    ns = "ns"

    us = "us"

    ms = "ms"

    s = "s"

class RunType(enum.StrEnum):
    """
    Distinguishes per-repetition runs from aggregate (mean / median / stddev) rows.
    """

    iteration = "iteration"

    aggregate = "aggregate"

class BenchmarkName:
    """
    A registered name split into its parts.
    Google Benchmark assembles these into the reported `function/args/min_time:...` string; `str()` renders it.
    """

    @property
    def function_name(self) -> str: ...
    @property
    def args(self) -> str: ...
    @property
    def min_time(self) -> str: ...
    @property
    def min_warmup_time(self) -> str: ...
    @property
    def iterations(self) -> str: ...
    @property
    def repetitions(self) -> str: ...
    @property
    def time_type(self) -> str: ...
    @property
    def threads(self) -> str: ...

class Run:
    """
    A single benchmark run report.
    Times are in seconds (accumulated across iterations); use `adjusted_real_time()` for per-iteration averages.
    Projected to a `RunRow` dict at the reporter boundary (`mew.reporter._run_to_dict`).
    """

    @property
    def run_name(self) -> BenchmarkName: ...
    def benchmark_name(self) -> str: ...
    @property
    def family_index(self) -> int: ...
    @property
    def per_family_instance_index(self) -> int: ...
    @property
    def run_type(self) -> RunType: ...
    @property
    def aggregate_name(self) -> str: ...
    @property
    def report_label(self) -> str: ...
    @property
    def skip_message(self) -> str: ...
    @property
    def iterations(self) -> int: ...
    @property
    def threads(self) -> int: ...
    @property
    def repetition_index(self) -> int: ...
    @property
    def repetitions(self) -> int: ...
    @property
    def time_unit(self) -> TimeUnit: ...
    @property
    def real_accumulated_time(self) -> float: ...
    @property
    def cpu_accumulated_time(self) -> float: ...
    def adjusted_real_time(self) -> float: ...
    def adjusted_cpu_time(self) -> float: ...
    @property
    def complexity_n(self) -> int: ...
    @property
    def counters(self) -> dict: ...
    @property
    def skipped(self) -> bool: ...

def run_benchmarks(
    argv: Sequence[str], reporter: object | None = None, extra_context: dict = {}
) -> int:
    """
    Initialize Google Benchmark with `argv` and run all registered benchmarks.
    Returns the number of benchmarks run.
    `extra_context` keys are overlaid onto the context dict passed to the reporter's `report_context` (session id/tag, user context).
    Pass a `Fanout` reporter to multiplex into multiple sinks.
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
        """
        Advance the iteration counter by `n`; return whether the budget permits another batch.
        Prefer `State.batches` for the idiomatic loop form.
        """

    def batches(self, n: int) -> BatchIter:
        """
        Return an iterator yielding `n` once per batch until the budget is spent.
        Use with a nested `for _ in range(n)` to amortize `__next__` dispatch for very fast bodies.
        Reported times include a small per-batch overshoot; do not mix with `for _ in state` results.
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
        self, name: str, value: float, flags: CounterFlags = CounterFlags.kDefaults
    ) -> None:
        """
        Attach a user-defined counter, surfaced in `RunRow['counters']`.
        `flags` controls how Google Benchmark normalizes it (see `CounterFlags`).
        """

    def range(self, pos: int = 0) -> int:
        """
        The `pos`-th range argument this benchmark was registered with.
        `@parametrize` / `@product` families use `range(0)` as the case index; the trampoline reads it to bind that case's kwargs and label.
        """

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
    Methods return the same handle so options can be chained.
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
        Requires a free-threaded interpreter: under the GIL the trampoline holds the GIL across Google Benchmark's per-thread start barrier, so the workers deadlock rather than run. On a GIL build mew warns and skips threaded benchmarks by default (see mew.run).
        """

    def thread_range(self, min_threads: int, max_threads: int) -> BenchmarkHandle:
        """
        Run the benchmark once per thread count in [min_threads, max_threads], stepping by the range multiplier (powers of two). See `threads` for the free-threading requirement.
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

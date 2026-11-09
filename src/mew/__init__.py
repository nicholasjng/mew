"""mew: microbenchmarking for Python via Google Benchmark."""

import atexit

from mew._console import Terminal
from mew._core import (
    BENCHMARK_COMMIT,
    BENCHMARK_VERSION,
    CounterFlags,
    CounterOneK,
    clear_registered_benchmarks as _clear_registered_benchmarks,
    unregister_memory_manager as _unregister_memory_manager,
    unregister_profiler_manager as _unregister_profiler_manager,
)
from mew._registry import REGISTRY, Entry, Registry
from mew._typing import (
    BenchmarkFn,
    BenchmarkResult,
    MemoryManager,
    MemoryMetrics,
    ProfilerManager,
    ProfilerSummary,
    RunInfo,
    SkipInfo,
    State,
)
from mew.api import benchmark, grid, parametrize
from mew.machine import machine_context, vcs_context
from mew.reporter import (
    Fanout,
    JSONLReporter,
    JSONReporter,
    Reporter,
    RichReporter,
)
from mew.runner import run

__version__ = "0.3.0"

# Registrations hold Python callables; release them while the interpreter is alive.
atexit.register(_clear_registered_benchmarks)
atexit.register(_unregister_memory_manager)
atexit.register(_unregister_profiler_manager)
del _clear_registered_benchmarks, _unregister_memory_manager, _unregister_profiler_manager

__all__ = [
    "BENCHMARK_COMMIT",
    "BENCHMARK_VERSION",
    "REGISTRY",
    "BenchmarkFn",
    "BenchmarkResult",
    "CounterFlags",
    "CounterOneK",
    "Entry",
    "Fanout",
    "JSONLReporter",
    "JSONReporter",
    "MemoryManager",
    "MemoryMetrics",
    "ProfilerManager",
    "ProfilerSummary",
    "Registry",
    "Reporter",
    "RichReporter",
    "RunInfo",
    "SkipInfo",
    "State",
    "Terminal",
    "__version__",
    "benchmark",
    "grid",
    "machine_context",
    "parametrize",
    "run",
    "vcs_context",
]

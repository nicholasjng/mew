"""mew: microbenchmarking for Python via Google Benchmark."""

import atexit

from mew._core import (
    BENCHMARK_COMMIT,
    BENCHMARK_VERSION,
    CounterFlags,
    CounterOneK,
    TimeUnit,
    clear_registered_benchmarks as _clear_registered_benchmarks,
)
from mew._registry import REGISTRY, Entry, Registry
from mew._typing import BenchmarkFn, BenchmarkResult, SessionInfo, State
from mew.api import benchmark, parametrize, product
from mew.context import clear_context, get_context, set_context, update_context
from mew.machine import machine_context
from mew.reporter import (
    Fanout,
    JSONLReporter,
    JSONReporter,
    Reporter,
    RichReporter,
)
from mew.runner import run
from mew.vcs import vcs_context

__version__ = "0.1.1"

# Clear Google Benchmark registrations at interpreter shutdown.
atexit.register(_clear_registered_benchmarks)
del _clear_registered_benchmarks

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
    "Registry",
    "Reporter",
    "RichReporter",
    "SessionInfo",
    "State",
    "TimeUnit",
    "__version__",
    "benchmark",
    "clear_context",
    "get_context",
    "machine_context",
    "parametrize",
    "product",
    "run",
    "set_context",
    "update_context",
    "vcs_context",
]

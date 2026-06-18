"""mew: microbenchmarking for Python via Google Benchmark."""

import atexit

from mew._core import (
    BENCHMARK_COMMIT,
    BENCHMARK_VERSION,
    TimeUnit,
    clear_registered_benchmarks as _clear_registered_benchmarks,
)
from mew._registry import REGISTRY, Entry, Registry
from mew._typing import BenchmarkFn, RunRow, State
from mew.api import benchmark, parametrize, product
from mew.context import clear_context, get_context, set_context, update_context
from mew.reporter import (
    Fanout,
    JSONLReporter,
    JSONReporter,
    Reporter,
    RichReporter,
)
from mew.runner import run

__version__ = "0.1.0"

# Clear Google Benchmark registrations at interpreter shutdown.
atexit.register(_clear_registered_benchmarks)
del _clear_registered_benchmarks

__all__ = [
    "BenchmarkFn",
    "Entry",
    "Fanout",
    "JSONLReporter",
    "JSONReporter",
    "REGISTRY",
    "Registry",
    "Reporter",
    "RichReporter",
    "RunRow",
    "State",
    "TimeUnit",
    "BENCHMARK_COMMIT",
    "BENCHMARK_VERSION",
    "__version__",
    "benchmark",
    "clear_context",
    "get_context",
    "parametrize",
    "product",
    "run",
    "set_context",
    "update_context",
]

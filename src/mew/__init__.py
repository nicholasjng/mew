"""mew — microbenchmarking for Python via Google Benchmark."""

import atexit
from importlib.metadata import PackageNotFoundError, version as _pkg_version

from mew._core import (
    BENCHMARK_COMMIT,
    BENCHMARK_VERSION,
    BenchmarkHandle,
    Run,
    RunType,
    TimeUnit,
    clear_registered_benchmarks as _clear_registered_benchmarks,
)
from mew._registry import REGISTRY, Entry, Registry
from mew._typing import BenchmarkFn, State
from mew.api import benchmark, parametrize, product
from mew.context import clear_context, get_context, set_context, update_context
from mew.reporter import (
    Fanout,
    JSONLReporter,
    JSONReporter,
    ParquetReporter,
    Reporter,
    RichReporter,
)
from mew.runner import run

try:
    __version__ = _pkg_version("mew")
except PackageNotFoundError:
    __version__ = "0.0.0+unknown"

# Clear Google Benchmark registrations at interpreter shutdown.
atexit.register(_clear_registered_benchmarks)
del _clear_registered_benchmarks

__all__ = [
    "BenchmarkFn",
    "BenchmarkHandle",
    "Entry",
    "Fanout",
    "JSONLReporter",
    "JSONReporter",
    "ParquetReporter",
    "REGISTRY",
    "Registry",
    "Reporter",
    "RichReporter",
    "Run",
    "RunType",
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

"""mew — microbenchmarking for Python via Google Benchmark."""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

from mew._core import (
    BENCHMARK_COMMIT,
    BENCHMARK_VERSION,
    BenchmarkHandle,
    Run,
    RunType,
    State,
    TimeUnit,
)
from mew._registry import REGISTRY, Entry, Registry
from mew.api import benchmark, parametrize, product
from mew.context import clear_context, get_context, set_context, update_context
from mew.reporter import Fanout, JSONReporter, ParquetReporter, Reporter, RichReporter
from mew.runner import run

try:
    __version__ = _pkg_version("mew")
except PackageNotFoundError:  # editable / source checkout without metadata
    __version__ = "0.0.0+unknown"

__all__ = [
    "BenchmarkHandle",
    "Entry",
    "Fanout",
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

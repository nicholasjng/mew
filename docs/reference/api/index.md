# Python API

```{toctree}
:maxdepth: 1

decorators
state
runner
reporters
compare
profiling
context
registry
config
regressions
```

## Top-level re-exports

The public surface lives at the package root; all names below are
importable from `mew` directly:

```{eval-rst}
.. currentmodule:: mew

.. autosummary::

   benchmark
   parametrize
   product
   State
   BenchmarkFn
   MemoryManager
   ProfilerManager
   ProfilerResultProvider
   PausableProfiler
   run
   Reporter
   RichReporter
   JSONReporter
   JSONLReporter
   Fanout
   BenchmarkResult
   SessionInfo
   MemoryMetrics
   TimeUnit
   CounterFlags
   CounterOneK
   Registry
   Entry
   REGISTRY
   BENCHMARK_VERSION
   BENCHMARK_COMMIT
   set_context
   update_context
   get_context
   gil_enabled
   clear_context
   vcs_context
   machine_context
```

{data}`mew.ProfilerSummary` describes the free-form dictionary returned by a
{class}`mew.ProfilerResultProvider`.

## Build information

```{eval-rst}
.. py:data:: BENCHMARK_VERSION
   :type: str

   The Google Benchmark version the extension was built against, in
   ``git describe`` form with ``+mew`` marking mew's patches, e.g.
   ``v1.9.5-144-ge662de9a+mew``.

.. py:data:: BENCHMARK_COMMIT
   :type: str

   The full hash of the pinned Google Benchmark commit.
```

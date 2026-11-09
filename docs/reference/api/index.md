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
   grid
   State
   BenchmarkFn
   MemoryManager
   ProfilerManager
   run
   Reporter
   RichReporter
   JSONReporter
   JSONLReporter
   Fanout
   Terminal
   BenchmarkResult
   RunInfo
   SkipInfo
   MemoryMetrics
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
   clear_context
   vcs_context
   machine_context
```

{data}`mew.ProfilerSummary` describes the free-form dictionary a
{class}`mew.ProfilerManager` returns from `get_result()`.

## Build information

```{eval-rst}
.. py:data:: BENCHMARK_VERSION
   :type: str

   The Google Benchmark version the extension was built against, in
   ``git describe`` form with ``+mew`` marking mew's patches, e.g.
   ``v1.10.0-4-g00aed1d2+mew``.

.. py:data:: BENCHMARK_COMMIT
   :type: str

   The full hash of the pinned Google Benchmark commit.
```

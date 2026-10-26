# Python API reference

Use this section to look up signatures, options, and error behavior.
For a complete script, start with [using mew from Python](../../guide/python-api.md).
The [result format](../results.md) documents the rows every reporter receives.

| Task | API |
| --- | --- |
| Register a benchmark or a family of inputs | {func}`mew.benchmark`, {func}`mew.parametrize`, {func}`mew.grid` |
| Control the loop, pause timing, or add counters | {class}`mew.State` |
| Select registered benchmarks | {data}`mew.REGISTRY`, {meth}`mew.Registry.filter` |
| Run benchmarks from a script | {func}`mew.run` |
| Save results or receive them in Python | {class}`mew.JSONReporter`, {class}`mew.JSONLReporter`, {class}`mew.Reporter` |
| Read result files or compare them | {func}`mew.compare.read_results`, {func}`mew.compare.compare` |
| Record a revision or workload metadata | {func}`mew.vcs_context`, the `context` argument of {func}`mew.run` |
| Apply a regression threshold | {class}`mew.regressions.RegressionConfig` |

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

The names below are importable directly from `mew`.
Comparison helpers live in `mew.compare`, regression rules in `mew.regressions`, and profiler implementations in `mew.cpu` and `mew.memory`.

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
   vcs_context
   machine_context
```

{data}`mew.ProfilerSummary` describes the JSON-serializable mapping returned by {meth}`mew.ProfilerManager.get_result`.

## Build information

```{eval-rst}
.. py:data:: BENCHMARK_VERSION
   :type: str

   The Google Benchmark version the extension was built against, in ``git describe`` form with ``+mew`` marking mew's patches, such as ``v1.10.0-13-g3251a251+mew``.

.. py:data:: BENCHMARK_COMMIT
   :type: str

   The full hash of the pinned Google Benchmark commit.
```

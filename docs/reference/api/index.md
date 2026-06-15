# Python API

```{toctree}
:maxdepth: 1

decorators
state
runner
reporters
context
registry
config
regressions
```

## Top-level re-exports

The public surface lives at the package root — all names below are
importable from `mew` directly:

```{eval-rst}
.. currentmodule:: mew

.. autosummary::

   benchmark
   parametrize
   product
   State
   BenchmarkFn
   run
   Reporter
   RichReporter
   JSONReporter
   ParquetReporter
   Fanout
   Run
   RunType
   TimeUnit
   BenchmarkHandle
   Registry
   Entry
   REGISTRY
   BENCHMARK_VERSION
   BENCHMARK_COMMIT
   set_context
   update_context
   get_context
   clear_context
```

# Reporters

A reporter implements `report_context(context)` and `report_results(results)`, with an optional `finalize()`.
Callbacks arrive on the main thread.
Pass a list to `run(reporter=[...])` to use several, which are called in order.
`run()` finalizes every reporter when it ends, and a callback exception stops the run.
Rows are {class}`BenchmarkResult` dictionaries in the [result format](../results.md).

`RichReporter(show_repetitions=False)` hides repeated rows from the terminal, and `show_memory=True` and `show_cpu=True` add profiling summaries.
Its width comes from `COLUMNS`, else the terminal, else 80 columns.
`JSONReporter` output becomes valid JSON at `finalize()`.
`JSONLReporter` lines stay usable after an interruption.

```{eval-rst}
.. currentmodule:: mew

.. autoclass:: BenchmarkResult
   :no-members:

.. autoclass:: RunInfo
   :no-members:

.. autoclass:: SkipInfo
   :no-members:

.. autoclass:: Reporter
   :no-members:

.. autoclass:: RichReporter
   :members:

.. autoclass:: JSONReporter
   :members:

.. autoclass:: JSONLReporter
   :members:

.. autoclass:: Terminal
   :members:
```

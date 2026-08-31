# Profiling

In-process profiling runs as Google Benchmark *managers*: `mew run` registers one
for the duration of the run, Google Benchmark drives an extra untimed pass of each
benchmark body per repetition, and the resulting figures are stamped onto that
repetition's `Run`. Reporters see them as the `memory` and `cpu_profile` blocks of
a {class}`~mew._typing.BenchmarkResult`.

Custom managers implement the {class}`mew.MemoryManager` or
{class}`mew.ProfilerManager` protocol. A profiler may additionally implement
{class}`mew.ProfilerResultProvider` and {class}`mew.PausableProfiler`.

```{eval-rst}
.. autoclass:: mew.memory.MemrayManager
   :members:

.. autofunction:: mew.memory.manager

.. autofunction:: mew.memory.write_flamegraph

.. autoclass:: mew.cpu.PyinstrumentManager
   :members:

.. autofunction:: mew.cpu.write_html
```

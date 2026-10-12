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
.. currentmodule:: mew

.. autoclass:: MemoryManager

.. autoclass:: ProfilerManager

.. autoclass:: ProfilerResultProvider

.. autoclass:: PausableProfiler

.. autoclass:: MemoryMetrics

.. py:data:: ProfilerSummary
   :type: dict[str, Any]

   Free-form summary a :class:`ProfilerResultProvider` returns from ``get_result()``;
   it lands in each row's ``cpu_profile`` block. Keys are strings and values
   anything :func:`json.dumps` accepts, ``None`` included, and they arrive with
   their JSON types. A value ``json.dumps`` rejects, such as a NumPy integer,
   fails the run with ``TypeError``.

.. autoclass:: mew.memory.MemrayManager
   :members:

.. autofunction:: mew.memory.manager

.. autofunction:: mew.memory.write_flamegraph

.. autofunction:: mew.cpu.manager

.. autoclass:: mew.cpu.TachyonManager
   :members:

.. autoclass:: mew.cpu.PyinstrumentManager
   :members:

.. autofunction:: mew.cpu.write_html

.. autoclass:: mew.cpu.CpuProfile

.. autoclass:: mew.cpu.TopFunction

.. autoclass:: mew.cpu.TopInstruction
```

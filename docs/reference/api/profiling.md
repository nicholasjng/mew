# Profiling

In-process profiling runs as Google Benchmark *managers*: `mew run` registers one
for the duration of the run, Google Benchmark drives an extra untimed pass of each
benchmark body per repetition, and the resulting figures are stamped onto that
repetition's `Run`. Reporters see them as the `memory` and `cpu_profile` blocks of
a {class}`mew.BenchmarkResult`.

Custom managers implement the {class}`mew.MemoryManager` or
{class}`mew.ProfilerManager` protocol. A profiler may additionally implement
{class}`mew.ProfilerResultProvider` and {class}`mew.PausableProfiler`.

- A memory manager's `stop()` returns {class}`mew.MemoryMetrics`, a dict of
  ints with `peak_bytes` and `total_allocations` and optionally `total_bytes`
  and `net_heap_growth`. `None` means it measured nothing, and the row gets no
  `memory` block. A missing required key, an unknown key or a value that isn't
  an int fails the run with `TypeError`.
- A profiler's `get_result()` summary becomes the row's `cpu_profile` block,
  even when empty; `None` leaves the block out.
- `pause()` and `resume()` come in pairs. When a body leaves a `state.pause()`
  scope open at the end of its loop, mew calls `resume()` before
  `before_teardown_stop()`, so a manager never stops while paused.

{func}`mew.cpu.write_html` renders the passes of mew's two samplers only, and
raises `TypeError` for any other manager. A sampler's pass that caught no sample
still gets a {class}`mew.cpu.CpuProfile`, with `sample_count` 0 and no
`top_function`.

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
   it lands in each row's ``cpu_profile`` block, an empty one included. Keys are
   strings and values anything :func:`json.dumps` accepts, ``None`` included, and
   they arrive with their JSON types. A value ``json.dumps`` rejects, such as a
   NumPy integer, fails the run with ``TypeError``.

.. autoclass:: mew.memory.MemrayManager
   :members:

.. autofunction:: mew.memory.manager

.. autofunction:: mew.memory.write_flamegraph

.. autofunction:: mew.cpu.manager

.. autodata:: mew.cpu.SAMPLERS

.. autoclass:: mew.cpu.TachyonManager
   :members:

.. autoclass:: mew.cpu.PyinstrumentManager
   :members:

.. autofunction:: mew.cpu.write_html

.. autofunction:: mew.cpu.reads_instructions

.. autoclass:: mew.cpu.CpuProfile

.. autoclass:: mew.cpu.TopFunction

.. autoclass:: mew.cpu.TopInstruction
```

# Profiling

See [profiling](../../howto/profiling.md) for CLI and Python examples.
Google Benchmark drives an extra, untimed pass per benchmark repetition using the registered managers.
Their measurements become the `memory` and `cpu_profile` blocks of a {class}`mew.BenchmarkResult`.

`mew run --cpu-profiler` and `--memory-profiler` load a manager by preset or class spec (see [profiler presets](../../howto/profiling.md#sample-cpu-time)), construct it with the preset's options as keyword arguments, and enter it for the run if it is a context manager.
Each flag fills one of Google Benchmark's slots and checks that the class defines that slot's methods: `after_setup_start()` and `before_teardown_stop()` for `--cpu-profiler`, `start()` and `stop()` for `--memory-profiler`.
From Python, pass managers to {func}`mew.run` as `profiler_manager=` and `memory_manager=`.

Custom managers implement the {class}`mew.MemoryManager` or {class}`mew.ProfilerManager` protocol.
Only starting and stopping are required.
A subclass of a protocol inherits the other hooks, which do nothing.

- A memory manager's `stop()` returns {class}`mew.MemoryMetrics`, a dict of ints with `peak_bytes` and `total_allocations` and optionally `total_bytes` and `net_heap_growth`.
  `None` means it measured nothing, and the row gets no `memory` block.
  A missing required key or a value that isn't an int fails the run with `TypeError`.
  Other keys are ignored.
- A profiler's `get_result()` summary becomes the row's `cpu_profile` block, even when empty.
  `None` leaves the block out.
- `pause()` and `resume()` come in pairs, and a manager never stops while paused.
- `write_outputs(directory)` writes a manager's files, such as HTML reports, after the run.
  `mew run --profile-dir DIR` passes `DIR/cpu` and `DIR/memory`.

A sampler's pass that caught no sample still gets a {class}`mew.cpu.CpuProfile`, with `sample_count` 0 and no `top_function`.

```{eval-rst}
.. currentmodule:: mew

.. autoclass:: MemoryManager

.. autoclass:: ProfilerManager

.. autoclass:: MemoryMetrics

.. py:data:: ProfilerSummary
   :type: Mapping[str, Any]

   Summary a :class:`ProfilerManager` returns from ``get_result()``.
   It lands in each row's ``cpu_profile`` block, an empty one included.
   It must be a ``dict``, a TypedDict such as :class:`mew.cpu.CpuProfile` included.
   Another mapping fails the run with ``TypeError``.
   Keys are strings and values anything :func:`json.dumps` accepts, ``None`` included.
   A value ``json.dumps`` rejects, such as a NumPy integer, fails the run with ``TypeError``.

.. autoclass:: mew.memory.MemrayManager
   :members:

.. autofunction:: mew.cpu.manager

.. autodata:: mew.cpu.SAMPLERS

.. autoclass:: mew.cpu.TachyonManager
   :members:

.. autoclass:: mew.cpu.PyinstrumentManager
   :members:

.. autoclass:: mew.cpu.CpuProfile

.. autoclass:: mew.cpu.TopFunction
```

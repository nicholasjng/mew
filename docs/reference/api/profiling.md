# Profiling

In-process profiling runs as Google Benchmark *managers*: `mew run` registers one
for the duration of the run, Google Benchmark drives an extra untimed pass of each
benchmark body per repetition, and the resulting figures are stamped onto that
repetition's `Run`. Reporters see them as the `memory` and `cpu_profile` blocks of
a {class}`mew.BenchmarkResult`.

`mew run --cpu-profiler` and `--memory-profiler` load a manager by preset or
class spec (see {doc}`../../guide/configuration`), construct it with the
preset's options as keyword arguments, and enter it for the run if it is a
context manager. Each flag fills one of Google Benchmark's slots, and checks
that the class has its methods: `after_setup_start()` and
`before_teardown_stop()` for `--cpu-profiler` (a profiler manager), `start()`
and `stop()` for `--memory-profiler` (a memory manager). They must be defined
on the class, not through `__getattr__`. From Python, pass managers to
{func}`mew.run` as `profiler_manager=` and `memory_manager=`.

Custom managers implement the {class}`mew.MemoryManager` or
{class}`mew.ProfilerManager` protocol. Only starting and stopping are
required: a subclass of a protocol inherits the other hooks, which do nothing,
and overrides the ones it supports. A manager that doesn't subclass may leave
out `get_result()`, `pause()` and `resume()`, which mew then doesn't call, but
type checkers expect every method of the protocol.

- A memory manager's `stop()` returns {class}`mew.MemoryMetrics`, a dict of
  ints with `peak_bytes` and `total_allocations` and optionally `total_bytes`
  and `net_heap_growth`. `None` means it measured nothing, and the row gets no
  `memory` block. A missing required key or a value that isn't an int fails
  the run with `TypeError`; other keys are ignored.
- A profiler's `get_result()` summary becomes the row's `cpu_profile` block,
  even when empty; `None` leaves the block out.
- `pause()` and `resume()` come in pairs. When a body leaves a `state.pause()`
  scope open at the end of its loop, mew calls `resume()` before
  `before_teardown_stop()`, so a manager never stops while paused.

- `write_outputs(directory)` writes a manager's files, such as HTML reports,
  after the run; `mew run --profile-dir DIR` passes `DIR/cpu` and
  `DIR/memory`. mew's samplers write their passes as one HTML report, `index.html`, and
  `MemrayManager` a flame graph per benchmark.

A sampler's pass that caught no sample still gets a {class}`mew.cpu.CpuProfile`,
with `sample_count` 0 and no `top_function`.

```{eval-rst}
.. currentmodule:: mew

.. autoclass:: MemoryManager

.. autoclass:: ProfilerManager

.. autoclass:: MemoryMetrics

.. py:data:: ProfilerSummary
   :type: Mapping[str, Any]

   Free-form summary a :class:`ProfilerManager` returns from ``get_result()``;
   it lands in each row's ``cpu_profile`` block, an empty one included. It must
   be a ``dict``, a TypedDict such as :class:`mew.cpu.CpuProfile` included;
   another mapping fails the run with ``TypeError``. Keys are
   strings and values anything :func:`json.dumps` accepts, ``None`` included, and
   they arrive with their JSON types. A value ``json.dumps`` rejects, such as a
   NumPy integer, fails the run with ``TypeError``.

.. autoclass:: mew.memory.MemrayManager
   :members:

.. autoclass:: mew.memory.Capture

.. autofunction:: mew.memory.manager

.. autofunction:: mew.cpu.manager

.. autodata:: mew.cpu.SAMPLERS

.. autoclass:: mew.cpu.TachyonManager
   :members:

.. autoclass:: mew.cpu.PyinstrumentManager
   :members:

.. autoclass:: mew.cpu.CpuProfile

.. autoclass:: mew.cpu.TopFunction

```

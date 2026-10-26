# `State`

The benchmark function receives a `State`, and iterating it runs the timed loop.
See [the timed loop](../../guide/writing-benchmarks.md#the-timed-loop) for pauses, batches, manual time, threads, and counters.

- `state.pause()` is valid only inside the loop, and outside it raises and skips the benchmark.
  A pause entered inside another does nothing, and re-entering the same scope object inside its own block raises.
- `state.batches(n)` may overshoot the iteration budget by up to `n - 1`, and per-iteration times use the actual count.
- With `use_manual_time=True`, call `state.set_iteration_time(seconds)` once per iteration.
- Counters are summed across threads and stored as floats.
- Profiling passes run one thread, where `state.threads` is 1 and `state.thread_index` is 0.

```{eval-rst}
.. currentmodule:: mew

.. autoclass:: State
   :members:

.. autoclass:: BenchmarkFn

.. autoclass:: CounterFlags
   :members:
   :undoc-members:

.. autoclass:: CounterOneK
   :members:
   :undoc-members:
```

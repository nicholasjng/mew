# State and timing

The `State` object exposes the parts of `benchmark::State` that Python users need.
Most benchmarks only use iteration:

```python
for _ in state:
    do_work()
```

## Batched iteration for very fast bodies

Each `for _ in state` step crosses the Python→C boundary via `__next__`.
For sub-100 ns bodies that dispatch is a meaningful share of the measured time.
`State.batches(n)` yields `n` once per batch, letting the inner loop run as pure-Python `for _ in range(n)`:

```python
@mew.benchmark
def bench_tight(state):
    a, b = 1, 2
    for n in state.batches(1024):
        for _ in range(n):
            a + b
```

:::{warning}
Batched and naive (`for _ in state`) timings are **not directly comparable** — the batched form removes per-iter dispatch overhead from the measurement.
Pick one style per benchmark and don't switch back and forth across releases.
A consistent `tags=("batched",)` is a good way to flag these runs for downstream comparison.
:::

The last batch may overshoot `max_iterations` by up to `n - 1` body executions.
GB reports the actual iteration count and divides by it, so per-iter time stays accurate — just expect a slightly higher total wall time when the body has visible side effects.
Keep `n` well below `max_iterations` (1024 is a reasonable default).

## Pausing the timer

{meth}`State.pause()` is a context manager that excludes its body from the measured region.
Use it to rebuild state every iteration without leaking the cost into the timing:

```python
import random

@mew.benchmark
def bench_shuffle_then_sort(state):
    for _ in state:
        with state.pause():
            data = list(range(1000))
            random.shuffle(data)
        sorted(data)
```

The `pause()` context manager keeps the timer in a consistent state even when the body raises.

## Repetitions vs iterations

- **Iterations** are the inner loop count chosen by Google Benchmark to
  reach `min_time`. Forcing this disables auto-tuning — only do it when
  comparing against an absolute baseline.
- **Repetitions** rerun the entire benchmark, including warm-up, _N_
  times so you get `_mean`, `_median`, `_stddev` aggregate rows.

Combine `repetitions=10` with `report_aggregates_only=True` if you only
care about the aggregates and want the per-rep rows hidden from output
sinks.

## Real vs. CPU time

Reporters print both `Real` and `CPU` columns; the difference is informative:

- `Real == CPU`: single-threaded, CPU-bound work.
- `Real > CPU`: the benchmark is sleeping, blocking on I/O, or contending on a lock.
- `Real < CPU`: multi-threaded — pair with `measure_process_cpu_time=True` if you want CPU time summed across threads.

Set `use_real_time=True` if your benchmark's primary metric is wall-clock time.

## Manual time

Some benchmarks need their own clock — e.g. GPU work where the CPU launches the kernel and waits asynchronously.
Set `use_manual_time=True` and call the state's manual-time setter inside the loop.
See the Google Benchmark docs for the exact contract.

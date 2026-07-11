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
Batched and naive (`for _ in state`) timings are **not directly comparable**: the batched form removes per-iter dispatch overhead from the measurement.
Pick one style per benchmark and don't switch back and forth across releases.
A consistent `tags=("batched",)` is a good way to flag these runs for downstream comparison.
:::

The last batch may overshoot `max_iterations` by up to `n - 1` body executions.
GB reports the actual iteration count and divides by it, so per-iter time stays accurate; just expect a slightly higher total wall time when the body has visible side effects.
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
  reach `min_time`. Forcing this disables auto-tuning; only do it when
  comparing against an absolute baseline.
- **Repetitions** rerun the entire benchmark, including warm-up, _N_
  times so you get `_mean`, `_median`, `_stddev` aggregate rows.

Combine `repetitions=10` with `report_aggregates_only=True` if you only
care about the aggregates and want the per-rep rows hidden from output
sinks.

:::{warning}
Only do that for output you read by eye. `mew compare` recomputes statistics
from the per-repetition rows and discards Google Benchmark's aggregate rows, so
an aggregates-only benchmark disappears from a comparison **silently** — no
warning, exit code 0. See [](trusting-results.md).
:::

## Real vs. CPU time

Reporters print both `Real` and `CPU` columns; the difference is informative:

- `Real == CPU`: single-threaded, CPU-bound work.
- `Real > CPU`: the benchmark is sleeping, blocking on I/O, or contending on a lock.
- `Real < CPU`: multi-threaded; pair with `measure_process_cpu_time=True` if you want CPU time summed across threads.

Set `use_real_time=True` if your benchmark's primary metric is wall-clock time.

## Threaded benchmarks (free-threading)

Google Benchmark can run a single benchmark body concurrently across _N_ threads.
mew exposes this through the `threads` and `thread_range` options:

```python
@mew.benchmark(threads=4)
def bench_parallel(state):
    lo, hi = partition(len(DATA), state.threads, state.thread_index)
    for _ in state:
        process(DATA[lo:hi])
```

Each thread gets its own `State` and timer; `state.threads` is the thread count
and `state.thread_index` is this thread's 0-based id; use them to partition work
so the threads don't all redo the same thing. `thread_range=(1, 8)` runs the
benchmark once per thread count `1, 2, 4, 8` so you can chart scaling.

:::{warning}
**Threaded mode requires a free-threaded interpreter (CPython 3.14t+).**
On a stock (GIL) interpreter the worker threads would deadlock on Google
Benchmark's start barrier rather than run. mew detects this up front: by default
it **warns and skips** the threaded benchmarks (emitting a `skipped` row for
each) and runs the rest, so a mixed suite still works on stock CPython. Run it
again on a free-threaded build to execute them. Pass `mew run --strict` (or
`mew.run(strict=True)`) to turn the skip into a `RuntimeError` — useful in CI
where the threaded benchmarks are the point and a silent skip would hide a
misconfiguration.
:::

Counters and labels follow Google Benchmark's convention: `set_counter` /
`set_items_processed` are summed across threads into the merged result, so set
per-thread values and let the merge total them, or guard one-shot calls with
`if state.thread_index == 0`. Thread-safety of the benchmark body itself is your
responsibility: _N_ threads invoke the same Python callable at once.

## Manual time

Some benchmarks need their own clock, e.g. GPU work where the CPU launches the kernel and waits asynchronously.
Set `use_manual_time=True` and call the state's manual-time setter inside the loop.
See the Google Benchmark docs for the exact contract.

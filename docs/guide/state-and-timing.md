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
Batched and ordinary iteration are not directly comparable. Use one style
consistently for a given benchmark.
:::

The last batch may exceed `max_iterations` by up to `n - 1` calls. Google
Benchmark reports the actual count, so per-iteration timing remains correct.

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

The context manager resumes timing when its body raises.

## Repetitions vs iterations

- **Iterations** are the inner-loop count. Google Benchmark chooses it to reach
  `min_time`; setting it explicitly disables auto-tuning.
- **Repetitions** rerun the benchmark, including warm-up, and produce aggregate rows.

Use `report_aggregates_only=True` to hide per-repetition rows.

:::{warning}
Do not use aggregates-only output with `mew compare`: it discards Google
Benchmark aggregates and needs the per-repetition rows.
:::

## Real vs. CPU time

Reporters print both `Real` and `CPU` columns; the difference is informative:

- `Real == CPU`: single-threaded, CPU-bound work.
- `Real > CPU`: the benchmark is sleeping, blocking on I/O, or contending on a lock.
- `Real < CPU`: multi-threaded; pair with `measure_process_cpu_time=True` if you want CPU time summed across threads.

Set `use_real_time=True` if your benchmark's primary metric is wall-clock time.

## Counters

`state.set_counter()` attaches a numeric measurement to the result. Use
{class}`mew.CounterFlags` for rates and other normalization, and set
`one_k=mew.CounterOneK.kIs1024` when human-readable output should use binary
rather than decimal prefixes.

## Threaded benchmarks (free-threading)

Google Benchmark can run a single benchmark body concurrently across _N_ threads.
mew exposes this through `threads`, `thread_range`, and `dense_thread_range`:

```python
@mew.benchmark(threads=4)
def bench_parallel(state):
    lo, hi = partition(len(DATA), state.threads, state.thread_index)
    for _ in state:
        process(DATA[lo:hi])
```

Each thread gets its own `State` and timer. Use `state.threads` and
`state.thread_index` to partition work. `thread_range=(1, 8)` runs at 1, 2, 4,
and 8 threads; `dense_thread_range=(1, 8, 1)` runs every count from 1 through 8.

:::{warning}
**Threaded mode requires a free-threaded interpreter (CPython 3.14t+).**
On a GIL build, mew warns and emits skipped rows because Google Benchmark's
worker barrier would deadlock. Use `--strict` (or `strict=True`) to raise instead.
:::

Counters are summed across threads. Set per-thread values, or guard one-time
updates with `if state.thread_index == 0`. The benchmark body must be thread-safe.

## Manual time

Some benchmarks need their own clock, e.g. GPU work where the CPU launches the kernel and waits asynchronously.
Set `use_manual_time=True` and call the state's manual-time setter inside the loop.
See the Google Benchmark docs for the exact contract.

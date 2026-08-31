# Writing benchmarks

## The `@mew.benchmark` decorator

`@mew.benchmark` is the smallest unit: one function, one registered benchmark.
Bare or called forms are both valid:

```python
@mew.benchmark
def bench_a(state): ...


@mew.benchmark(min_time=0.5, tags="sort")
def bench_b(state): ...
```

See {func}`mew.benchmark` for the full signature.

## Per-benchmark options

All options are optional and map 1:1 to Google Benchmark concepts:

| Option                       | Effect                                                       |
| ---------------------------- | ------------------------------------------------------------ |
| `min_time`                   | Seconds Google Benchmark may spend stabilising the timing.    |
| `min_warmup_time`            | Seconds spent warming caches before measurement.              |
| `iterations`                 | Force an exact iteration count (skips auto-tuning).           |
| `repetitions`                | Re-run the whole benchmark _N_ times for variance metrics.    |
| `unit`                       | Override reported time unit (`"ns"`, `"us"`, `"ms"`, `"s"`).  |
| `use_real_time`              | Report wall-clock instead of CPU time.                        |
| `use_manual_time`            | The benchmark calls `state.set_iteration_time()` itself.      |
| `measure_process_cpu_time`   | Use process-wide CPU time (multi-threaded benchmarks).        |
| `report_aggregates_only`     | When `repetitions > 1`, suppress per-rep rows.                |
| `threads`                    | Run the body with _N_ threads (free-threaded only; see below). |
| `thread_range`               | `(min, max)`: run once per thread count, powers of two.        |
| `dense_thread_range`         | `(min, max, stride)`: run at evenly spaced thread counts.      |

Per-benchmark decorator options take precedence over the global `mew run` flags (`--min-time`, `--repetitions`, ...).
See [](configuration.md).

## Naming

Auto-derived names mirror pytest node ids: `path/to/file.py::qualname`.
Override:

```python
@mew.benchmark(name="custom_name")
def whatever(state): ...
```

Variants from `@parametrize` / `@product` always get a `[label]` suffix.

## Tags

`mew run --tag` filters by tag.
Pass a single string or any iterable:

```python
@mew.benchmark(tags="hot-path")  # a single tag
def bench_lookup(state): ...


@mew.benchmark(tags=("hot-path", "sort"))  # several
def bench_sort(state): ...
```

Tag filters use OR semantics: `mew run --tag a --tag b` runs anything tagged as `a` or `b`.
Combine with `-k` for AND across tag and name.

## Common pitfalls

- **Setup inside the loop.** The body of `for _ in state:` is the measured
  region. Move data construction, file reads, and randomization outside
  the loop, or wrap them in {meth}`State.pause`.
- **Measuring nothing.** There is no `DoNotOptimize` to reach for, because
  CPython won't elide a call: `sorted(data)` as a bare statement still compiles
  to `CALL` + `POP_TOP`. What *does* vanish is constant work — `2 + 3` is folded
  at compile time and the statement disappears entirely, leaving a loop that
  measures the loop. The subtler versions are timing an `lru_cache` hit instead
  of the computation behind it, and timing an attribute lookup instead of the
  call. If the body really is a few nanoseconds, per-iteration dispatch
  dominates the measurement; use
  [](state-and-timing.md#batched-iteration-for-very-fast-bodies) rather than
  trying to defeat an optimiser that isn't there.
- **One decorator per function.** Applying both `@benchmark` and
  `@parametrize` to the same function raises a `RuntimeError` at import
  time. Split into two functions if you need both shapes.

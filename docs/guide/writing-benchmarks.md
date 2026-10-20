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
| `min_time`                   | Seconds Google Benchmark may spend stabilising the timing (`0.5`, `"500ms"`, `"1m"`), or a fixed iteration count like `"100x"`, the same as `iterations=100`. |
| `min_warmup_time`            | Seconds spent warming caches before measurement.              |
| `iterations`                 | Force an exact iteration count (skips auto-tuning); excludes `min_time` and `min_warmup_time`. |
| `repetitions`                | Re-run the whole benchmark _N_ times for variance metrics.    |
| `unit`                       | Override reported time unit (`"ns"`, `"us"`, `"ms"`, `"s"`).  |
| `use_real_time`              | Report wall-clock instead of CPU time.                        |
| `use_manual_time`            | The benchmark calls `state.set_iteration_time()` itself; excludes `use_real_time`. |
| `measure_process_cpu_time`   | Use process-wide CPU time (multi-threaded benchmarks).        |
| `threads`                    | Run the body with _N_ threads, or once per count in a sequence (free-threaded only). |

mew checks the options when the decorator is applied, so a mistake fails at import, not during a run.
The rules are the same as for `mew.run()`: a value of the wrong type, such as `repetitions=True` or `iterations=2.5`, raises `TypeError`.
A value out of range, such as `repetitions=0` or `unit="usec"`, or two options that exclude each other raise `ValueError`.

Per-benchmark decorator options take precedence over the global `mew run` flags (`--min-time`, `--repetitions`, ...), with one exception: `--min-time 100x` also fixes the iteration count of a benchmark with its own `min_time` in seconds.
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
  the loop, or wrap them in {meth}`mew.State.pause`.
- **Measuring nothing.** CPython does not elide calls, so there is no
  `DoNotOptimize`; `sorted(data)` as a bare statement still runs. Constant
  expressions such as `2 + 3` are folded at compile time, leaving a loop that
  measures only itself. Also watch for timing an `lru_cache` hit instead of the
  computation, or an attribute lookup instead of the call. If the body really is
  a few nanoseconds, per-iteration dispatch dominates; use
  [](state-and-timing.md#batched-iteration-for-very-fast-bodies).
- **One decorator per function.** Applying both `@benchmark` and
  `@parametrize` to the same function raises a `RuntimeError` at import
  time. Split into two functions if you need both shapes.

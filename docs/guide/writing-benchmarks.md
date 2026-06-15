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

Defaults live in your `pyproject.toml` `[tool.mew.benchmark_options]` and are overridden by per-decorator options and by CLI flags.
See [](configuration.md).

## Naming

Auto-derived names mirror pytest node ids — `path/to/file.py::qualname`.
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
@mew.benchmark(tags="hot-path")
@mew.benchmark(tags=("hot-path", "sort"))
```

Tag filters use OR semantics: `mew run --tag a --tag b` runs anything tagged as `a` or `b`.
Combine with `-k` for AND across tag and name.

## Common pitfalls

- **Setup inside the loop.** The body of `for _ in state:` is the measured
  region. Move data construction, file reads, and randomization outside
  the loop, or wrap them in {meth}`State.pause`.
- **Optimised-away work.** Python isn't as aggressive as C++ here, but
  expressions whose results are never bound can still be elided. Assign
  to a variable or pass to `state.consume(...)` when available.
- **One decorator per function.** Applying both `@benchmark` and
  `@parametrize` to the same function raises a `RuntimeError` at import
  time. Split into two functions if you need both shapes.

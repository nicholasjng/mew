# Configuration

`mew` resolves a `[tool.mew]` table from the nearest `pyproject.toml`; all keys are optional.

```toml
[tool.mew]
# Default paths for `mew run` / `mew list` when no positional args are given.
benchpaths = ["benchmarks"]
# Glob patterns for benchmark file discovery.
python_files = ["bench_*.py", "*_bench.py"]

[tool.mew.benchmark_options]
# Sticky Google Benchmark flags applied to every `mew run`.
# CLI-supplied flags appear later in argv and override these.
min_time = 0.5
repetitions = 5
```

## Resolution order (last wins)

1. Defaults from {class}`mew.config.Config`.
2. `[tool.mew.benchmark_options]` keys, formatted as `--benchmark_<key>[=value]`.
3. CLI flags: `--min-time`, `--repetitions`, `-o`/`--output`.
4. `--benchmark-option` raw passthrough — for anything `mew` doesn't model directly.

## `benchmark_options` keys

Keys are the short Google Benchmark flag names (kebab-case here, coerced before the `--benchmark_` flag is built). Only **measurement** flags apply — mew installs its own reporter, so Google Benchmark's *display/output* flags (`format`, `out`, `color`, `display-aggregates-only`) are ignored; some, like `out`, would even write a stray second file in GB's format. Use `--format` for stdout shape and `-o` for output sinks instead.

| Key                      | Effect                                                          |
| ------------------------ | --------------------------------------------------------------- |
| `min-time`               | `--benchmark_min_time=<value>` (seconds, or `<N>x` iterations)  |
| `min-warmup-time`        | Warm up for this long before timing                             |
| `repetitions`            | Repeat each benchmark N times (variance metrics need ≥ 2)       |
| `iterations`             | Force a fixed iteration count                                   |
| `report-aggregates-only` | Emit only aggregate rows (mean/median/stddev), not per-rep ones |

## Picking sensible defaults

- Set `min_time = 0.5` (or higher) for CI runs where you want stable timings.
- Local iteration: leave it at Google Benchmark's default for faster feedback.
- Set `repetitions = 5` if you compare with `mew compare` — variance metrics depend on it.

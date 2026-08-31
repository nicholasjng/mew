# Configuration

`mew` reads `[tool.mew]` from the nearest `pyproject.toml`. All keys are optional.
Both kebab-case and snake_case spellings are accepted.

```toml
[tool.mew]
# Default paths for `mew run` / `mew list` when no positional args are given.
benchpaths = ["benchmarks"]
# Glob patterns for benchmark file discovery.
python-files = ["bench_*.py", "*_bench.py"]
# Default reducer `mew compare` applies over per-repetition values: min, max,
# mean, median, gmean, or a pNN percentile like p95.
# Omit to keep the median; --statistic wins.
statistic = "median"

# Python file imported once, before any benchmark file. Relative to this
# pyproject.toml, so it applies from any working directory.
setup = "benchmarks/conf.py"
```

## Where measurement settings live

The config file holds project settings such as discovery paths and regression
rules. Configure measurements in decorators or CLI flags:

- **Per benchmark** — decorator options (`min_time=`, `repetitions=`,
  `iterations=`, `unit=`, …). These take precedence over global flags.
- **Per invocation** — `mew run` flags (`--min-time`, `--min-warmup-time`,
  `--repetitions`, `--random-interleaving`). Use a task runner or CI
  configuration for persistent invocation defaults.

## Picking sensible defaults

- Pass `--min-time 0.5` (or higher) in CI runs where you want stable timings.
- Local iteration: leave it at Google Benchmark's default for faster feedback.
- Pass `--repetitions 5` if you compare with `mew compare`; variance metrics
  depend on it, and `--random-interleaving` decorrelates the repeats from
  thermal/load drift.

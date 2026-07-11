# Configuration

`mew` resolves a `[tool.mew]` table from the nearest `pyproject.toml`; all keys are optional. Keys are kebab-case (`python-files`); mew coerces them to its snake-case fields, so the underscore spelling is also accepted.

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

The config file holds *project* settings (discovery paths, session tagging,
regression rules); measurement settings deliberately live elsewhere, where they
are visible next to what they affect:

- **Per benchmark** — decorator options (`min_time=`, `repetitions=`,
  `iterations=`, `unit=`, …), versioned with the benchmark itself. These take
  precedence over any global flag.
- **Per invocation** — `mew run` flags (`--min-time`, `--min-warmup-time`,
  `--repetitions`, `--random-interleaving`), so a result's provenance is the
  command that produced it. For persistent invocation defaults, use your task
  runner (justfile, Makefile, CI yaml) — the flags stay visible at the call
  site.

## Picking sensible defaults

- Pass `--min-time 0.5` (or higher) in CI runs where you want stable timings.
- Local iteration: leave it at Google Benchmark's default for faster feedback.
- Pass `--repetitions 5` if you compare with `mew compare`; variance metrics
  depend on it, and `--random-interleaving` decorrelates the repeats from
  thermal/load drift.

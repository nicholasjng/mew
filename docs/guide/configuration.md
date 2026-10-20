# Configuration

`mew` reads `[tool.mew]` from the nearest `pyproject.toml`. All keys are optional.
Keys are spelled with dashes, as TOML has it: `python-files`, not `python_files`.

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

# What `mew run` does with benchmark stdout/stderr: "fd" prints it after the
# run, "no" shows it live. Omit to keep "fd"; --capture wins.
capture = "fd"
```

`mew run`, `mew list`, and `mew compare` check the table before they start and
stop with exit code 2 on an error. An unknown key is an error, so a typo such as
`benchpath` does not silently leave the default in place:

```console
$ mew list
mew: invalid config: /path/to/project/pyproject.toml: unknown key 'benchpath' in [tool.mew]; did you mean 'benchpaths'?
```

An invalid `statistic` also stops `mew run`, not only `mew compare`, and so
does an error in `[tool.mew.regressions]` or its allowlist rules (see
{doc}`regressions`).

## Where measurement settings live

The config file holds project settings such as discovery paths and regression
rules. Configure measurements in decorators or CLI flags:

- **Per benchmark** — decorator options (`min_time=`, `repetitions=`,
  `iterations=`, `unit=`, …). These take precedence over global flags, except
  that a fixed iteration count such as `--min-time 100x` also replaces a
  decorator's `min_time` in seconds.
- **Per invocation** — `mew run` flags (`--min-time`, `--min-warmup-time`,
  `--repetitions`, `--random-interleaving`). Use a task runner or CI
  configuration for persistent invocation defaults.

## Picking sensible defaults

- `--min-time` defaults to 0.5 s per benchmark. Raise it (e.g. `--min-time 2`)
  in CI runs where you want more stable timings; lower it (`--min-time 0.1`)
  locally for faster feedback.
- Pass `--repetitions 5` if you compare with `mew compare`; variance metrics
  depend on it, and `--random-interleaving` decorrelates the repeats from
  thermal/load drift.

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
# mean, median, or gmean.
# Omit to keep the median; --statistic wins.
statistic = "median"

# Python file imported once, before any benchmark file. Relative to this
# pyproject.toml, so it applies from any working directory.
setup = "benchmarks/conf.py"
```

`mew run`, `mew list`, and `mew compare` check the table before they start and
stop with exit code 2 on an error. An unknown key is an error, so a typo such as
`benchpath` does not silently leave the default in place:

```console
$ mew list
mew: invalid config: /path/to/project/pyproject.toml: unknown key 'benchpath' in [tool.mew]; did you mean 'benchpaths'?
```

An invalid `statistic` also stops `mew run`, not only `mew compare`, and so
does an error in `[tool.mew.regressions]` or its allowlist rules, in
`[[tool.mew.counters]]` (see {doc}`regressions`), or in a profiler preset.

## Profiler presets

A `[tool.mew.profilers.<name>]` table is a preset that `mew run --cpu-profiler <name>`
or `--memory-profiler <name>` runs. Its `class` names the manager as `module:Class`, or as `path.py:Class`
relative to this `pyproject.toml`; every other key is a keyword argument for
the class, dashes turned into underscores.

```toml
[tool.mew.profilers.fine]
class = "mew.cpu:TachyonManager"
interval = 1e-5
top-k = 10

[tool.mew.profilers.counters]
class = "benchmarks/profilers.py:PerfCounters"
events = ["instructions", "cycles"]
```

```console
$ mew run --cpu-profiler fine --memory-profiler memray
```

- A preset can't be named after a built-in, `sample` or `memray`, and its name
  takes letters, digits, `-` and `_`.
- An option the class doesn't take stops the run with exit code 2 and the
  closest option name, before any benchmark runs.
- `func` is reserved for a later release, which may let a function choose the
  class; use a class that picks an implementation in `__init__` until then.
- The class's methods make it a memory manager or a profiler manager; a run
  takes one of each. See {doc}`../reference/api/profiling` for the contract.
- Every row records what ran in its context, as
  `profilers.<profiler|memory> = {preset, class, options}`, so `mew compare`
  shows where two files differ.

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

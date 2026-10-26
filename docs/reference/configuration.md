# Configuration

The CLI reads `[tool.mew]` from the nearest `pyproject.toml`.
Every key is optional and spelled with dashes.
Paths are relative to that `pyproject.toml`.
`mew.run()` from Python ignores the file and uses its arguments and the registered options.

```toml
[tool.mew]
benchpaths = ["benchmarks"]                # searched when no path is given
python-files = ["bench_*.py", "*_bench.py"]  # file name patterns for discovery
setup = "benchmarks/conf.py"               # imported before any benchmark file
```

| Table | Purpose | Documented in |
| --- | --- | --- |
| `[tool.mew.regressions]`, `[[tool.mew.regressions.allow]]` | Default threshold and per-benchmark exceptions | [Allowlist](../guide/comparing.md#allowlist) |
| `[[tool.mew.counters]]` | Which direction is better per counter | [Counters](../guide/comparing.md#counters) |
| `[tool.mew.profilers.<name>]` | Profiler presets for `--cpu-profiler` and `--memory-profiler` | [Profiling](../howto/profiling.md#sample-cpu-time) |

`mew run`, `mew list`, and `mew compare` check the whole table before they start.
An unknown key or invalid value stops them with exit code 2 and suggests the closest key:

```console
$ mew list
mew: error: invalid config: /path/to/project/pyproject.toml: unknown key 'benchpath' in [tool.mew]
hint: did you mean 'benchpaths'?
```

Measurement settings such as `min_time` and `repetitions` are not configuration.
Set them on decorators or pass them to `mew run`, and keep persistent invocations in a task runner or CI.

(setup-file)=
## Setup file

mew imports the `setup` file before discovery on every invocation, so it is the place for setup every run needs, such as environment variables.
If it defines `context()`, `mew run` calls it once before the first benchmark and records the returned dict in every row:

```python
# benchmarks/conf.py
import os

import mew

os.environ.setdefault("OMP_NUM_THREADS", "1")


def context() -> dict:
    return {**mew.vcs_context(), "ci": {"job": os.environ.get("CI_JOB_ID")}}
```

`mew list` does not call the hook, so listing never runs Git.
A hook that raises or returns anything but a dict with string keys stops the run with exit code 2.
Every selected benchmark gets the same context, and each row records its own case in `params`.
`mew run --context path=value` adds a string for one run, over what `context()` returns.
A dot nests, so `--context ci.job=42` sets `job` in the hook's `ci` table and keeps its other keys.
Quote a key that holds a dot: `--context '"a.b"=1'`.
To have `mew compare` warn when machines differ by name, return a `host`, such as `{"host": os.environ["RUNNER_NAME"]}`.

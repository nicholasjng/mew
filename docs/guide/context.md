# Context

Use context to record what you need to interpret or filter results later: a
commit, a dataset, a build flavour. From Python, pass it to {func}`mew.run`:

```python
import mew

mew.run(context={"git_sha": "abc123", "dataset": {"name": "uniform-1k", "size": 1000}})
```

From the CLI, a setup file's `context()` hook supplies it, and `--context`
adds to it for one run; see [below](#the-context-hook).

mew snapshots the context when the run starts.
`run` identifies the run; `context` holds user and machine provenance. Every
row of a result file carries both blocks:

```text
{
  "schema_version": 1,
  "run": {
    "id": "01975f2e-9c40-7b31-a1d4-8f0e2c5b7a90",
    "date": "2026-05-19T10:00:00.000Z",
    "name": "before"
  },
  "context": {
    "mew": "0.4.0",
    "cpu_model": "Apple M1 Pro",
    "num_cpus": 10,
    "cpu_scaling_enabled": false,
    "python": {"version": "3.15.0", "implementation": "cpython", "gil_enabled": true},
    "git_sha": "abc123",
    "dataset": {"name": "uniform-1k", "size": 1000}
  },
  "name": "benchmarks/bench_sort.py::bench_sort",
  ...
}
```

`context.mew` is the mew that ran the benchmarks, so `mew compare` warns when
two files come from different versions. `context.cpu_model` is the name the OS
gives the CPU, or `null` where it gives none, as on Linux on Arm.

`context.python` tells apart results from several interpreters in one archive,
such as a suite run on 3.14 and on free-threaded 3.15t. `mew compare` labels
each column with its version when the versions differ.

(run-identity)=
## Run identity

Every run gets a time-ordered UUIDv7 ID and its start date, in UTC with
milliseconds, stored on each row.

Name a run with `--run-name` or `run_name=`; `mew log`, `mew show` and
`mew compare` show the name, and the DuckDB recipes in {doc}`reporters` select
runs by it. Benchmark selection tags (`-t`) are unrelated.

(the-context-hook)=
## The `context()` hook

`mew run` takes its context from the setup file that `[tool.mew] setup` names.
mew imports that file before discovery, every time, so it is also the place for
setup every run needs, such as environment variables. If it defines
`context()`, `mew run` calls it once, after discovering the benchmarks and
before running them:

```toml
[tool.mew]
setup = "benchmarks/conf.py"
```

```python
# benchmarks/conf.py
import os

import mew

os.environ.setdefault("OMP_NUM_THREADS", "1")  # plain setup, at import


def context() -> dict:
    return {**mew.vcs_context(), "ci": {"job": os.environ.get("CI_JOB_ID")}}
```

- The path is relative to `pyproject.toml`.
- `mew list` doesn't call the hook, so listing never shells out to git or jj.
- A hook that raises, or returns anything but a dict with string keys, stops
  `mew run` with exit code 2 before any benchmark runs.
- Every selected benchmark gets the same context, whichever files the run
  imported. For data that differs per case, such as an input size, use
  `@mew.parametrize`: each row records its case in `params`.

### One run's values: `--context`

`--context <key>=<value>` adds a value to one run's context, over what
`context()` returns, for what differs between invocations of the same suite,
such as the side of an A/B comparison:

```console
$ mew run bench_alpha.py --context engine="alpha 1.5.3" -o alpha.json
```

It repeats, and a later value for a key wins. Values stay strings, `8` and
`true` included, since the shell can't say whether you meant a number, and a
key is used as written: `--context ci.job=42` records the key `ci.job`, not a
nested `ci` table. Compute anything richer in `context()`.

(recording-the-host)=
### Recording the host

mew doesn't record the machine's host name, since it often names a person, as
on macOS, and result files get shared. The CPU and interpreter fields describe
what timings depend on. To tell machines apart anyway, such as CI runners,
return a name from the hook:

```python
import socket


def context() -> dict:
    return {"host": socket.gethostname()}  # or os.environ["RUNNER_NAME"]
```

`mew compare` then warns when files differ in `host`, as it does for the CPU.

## Values

Nested dicts and lists are kept, so a DuckDB query reaches into them:

```sql
SELECT context.dataset.size AS size, real_time
FROM 'results.jsonl';
```

A value JSON can't hold is stored as its type's qualified name, such as
`"torch.Tensor"`, never as its `str()`, which for a large object can run to
megabytes. A path is stored as its string, a tuple as a list, and NaN and
infinity as JSON's `NaN` and `Infinity` (see [](reporters.md)).

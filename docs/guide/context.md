# Context

Use context to record metadata needed to interpret or filter results.

```python
import mew

mew.update_context(
    git_sha="abc123",
    dataset={"name": "uniform-1k", "size": 1000},
)
```

{func}`mew.run` snapshots context at startup.
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

Name a run with `--run-name` or `run_name=`; `mew compare` and `mew show`
select the newest run of that name in a file that holds several
(`results.jsonl@before`), and `mew log` lists them. The name `latest` is
reserved: `results.jsonl@latest` selects the newest run. Benchmark selection
tags (`-t`) are unrelated.

## Applying context to every run

Context is run-wide, but only selected benchmark files are imported.
Put shared setup in a dedicated file:

Point `[tool.mew] setup` at a file mew imports before discovery, every time:

```toml
[tool.mew]
setup = "benchmarks/conf.py"
```

```python
# benchmarks/conf.py
import os

import mew

mew.update_context(mew.vcs_context())
mew.set_context("ci.job", os.environ.get("CI_JOB_ID"))
```

The path is relative to `pyproject.toml` and is imported before discovery.

(recording-the-host)=
### Recording the host

mew doesn't record the machine's host name, since it often names a person, as
on macOS, and result files get shared. The CPU and interpreter fields describe
what timings depend on. To tell machines apart anyway, such as CI runners,
record a name in the setup file:

```python
import socket

mew.set_context("host", socket.gethostname())  # or os.environ["RUNNER_NAME"]
```

`mew compare` then warns when files differ in `host`, as it does for the CPU.

## Dotted keys

Dotted keys produce nested dictionaries:

```python
mew.set_context("dataset.size", 1000)
mew.set_context("dataset.kind", "uniform")
# -> {"dataset": {"size": 1000, "kind": "uniform"}}
```

The last write wins. Setting `dataset` replaces everything nested under it, and
setting `dataset.size` replaces a `dataset` value that is not a dict, `None`
included, with a new dict. Values are copied when you set them, so changing a
dict or list you passed changes neither the context nor, through later writes
to the context, your own object.

A DuckDB query on the resulting run data might look like:

```sql
SELECT context.dataset.size AS size, real_time
FROM 'results.jsonl';
```

## API surface

- {func}`mew.set_context`: set one key (dotted keys produce a nested struct).
- {func}`mew.update_context`: set many keys at once.
- {func}`mew.get_context`: snapshot the current state (deep-copied).
- {func}`mew.clear_context`: wipe all entries.

Prefer JSON-native values; unsupported objects are stringified.

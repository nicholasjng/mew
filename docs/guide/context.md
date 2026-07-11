# Context

Use the context API to stamp a run with metadata: git SHA, dataset identifier, hardware tag, anything you'd later want to filter on.

```python
import mew

mew.update_context(
    git_sha="abc123",
    dataset={"name": "uniform-1k", "size": 1000},
)
```

The snapshot is taken when {func}`mew.run` starts, so concurrent mutations don't affect an in-flight run.
A run's block has two halves. `session` is identity — what `mew compare` orders,
groups and addresses runs by. `context` is provenance: your values, alongside
whatever providers contributed. There is no privileged tier; the machine keys
below come from {func}`mew.machine_context`, which `mew run` applies by default.

```text
{
  "context": {
    "session": {
      "id": "01975f2e-9c40-7b31-a1d4-8f0e2c5b7a90",
      "date": "2026-05-19T10:00:00+00:00",
      "host": "laptop"
    },
    "context": {
      "num_cpus": 10,
      "cpu_scaling_enabled": false,
      "git_sha": "abc123",
      "dataset": {"name": "uniform-1k", "size": 1000}
    }
  },
  "benchmarks": [...]
}
```

Because provenance is one namespace, a new field never widens the row schema:
JSONL rows carry the same two keys however much either half grows.

## Session identity

Every {func}`mew.run` invocation is one *session* with a generated `session_id` (a time-ordered UUIDv7), persisted in the JSON context block and stamped onto every JSONL row.
This keeps runs distinguishable when several land in one archive, even two in the same wall-clock second.

`session.tag` is the optional label next to it: pass `mew run --session-tag before` (or `session_tag=` on {func}`mew.run`).
It is never derived — record what you built from with {func}`mew.vcs_context` instead, which lands in `context.vcs` and is what `compare` groups by when no tag is set.
Note `--session-tag` labels the run's *output*, unrelated to `-t/--tag`, which selects which benchmarks run.

## Applying context to every run

Context is run-wide: one `mew.update_context` call in any imported file covers
the whole run. But *which* files get imported depends on the invocation, so
`mew run benchmarks/bench_b.py` would miss a provider declared in `bench_a.py`.

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

It is ordinary Python, imported first — a "provider" is just a function you
call, so there is no protocol to implement or register. The path resolves
against the `pyproject.toml`, and a missing file is an error rather than a
silently context-less run.

## Dotted keys

Dotted keys produce nested dicts, handy for SQL drill-downs against a JSONL archive:

```python
mew.set_context("dataset.size", 1000)
mew.set_context("dataset.kind", "uniform")
# -> {"dataset": {"size": 1000, "kind": "uniform"}}
```

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

JSON-friendly values are strongly preferred: the file sinks serialize with `str()` by default, which loses information.

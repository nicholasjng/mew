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
`session` identifies the run; `context` holds user and machine provenance.

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

JSONL rows carry both blocks directly.

## Session identity

Every run gets a time-ordered UUIDv7 session ID, stored in JSON context and on each JSONL row.

Set `session.tag` with `--session-tag` or `session_tag=`. Untagged runs can be
grouped by `context.vcs.commit`. Benchmark selection tags (`-t`) are unrelated.

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

## Dotted keys

Dotted keys produce nested dictionaries:

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

Prefer JSON-native values; unsupported objects are stringified.

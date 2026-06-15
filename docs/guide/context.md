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
Reporters receive it under `ctx["custom"]`:

```text
{
  "context": {
    "date": "2026-05-19T10:00:00+00:00",
    "host_name": "laptop",
    "session_id": "01975f2e-9c40-7b31-a1d4-8f0e2c5b7a90",
    "session_tag": "v0.3.1-12-gabc123",
    "custom": {
      "git_sha": "abc123",
      "dataset": {"name": "uniform-1k", "size": 1000}
    }
  },
  "benchmarks": [...]
}
```

## Session identity

Every {func}`mew.run` invocation is one *session* with a generated `session_id` (a time-ordered UUIDv7), persisted in the context block (JSON/JSONL) or as per-row columns (Parquet).
This keeps runs distinguishable when several land in one archive — even two in the same wall-clock second.

`session_tag` is the human label next to it: pass `mew run --session-tag before` (or `session_tag=` on {func}`mew.run`).
With no tag, the CLI derives one from `git describe --always --dirty`; opt out with `auto_session_tag = false` under `[tool.mew]`.
Note `--session-tag` labels the run's *output* — unrelated to `-t/--tag`, which selects which benchmarks run.

## Dotted keys

Dotted keys produce nested dicts — handy for SQL drill-downs against the
Parquet sink:

```python
mew.set_context("dataset.size", 1000)
mew.set_context("dataset.kind", "uniform")
# -> {"dataset": {"size": 1000, "kind": "uniform"}}
```

A DuckDB query on the resulting run data might look like:

```sql
SELECT json_extract(custom, '$.dataset.size') AS size, real_time
FROM 'results.parquet';
```

## API surface

- {func}`mew.set_context` — set one key (dotted keys produce a nested struct).
- {func}`mew.update_context` — set many keys at once.
- {func}`mew.get_context` — snapshot the current state (deep-copied).
- {func}`mew.clear_context` — wipe all entries.

JSON-friendly values are strongly preferred — the JSON and Parquet sinks serialize with `str()` by default, which loses information.

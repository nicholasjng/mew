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
    "custom": {
      "git_sha": "abc123",
      "dataset": {"name": "uniform-1k", "size": 1000}
    }
  },
  "benchmarks": [...]
}
```

## Dotted keys

Dotted keys produce nested dicts — handy for SQL drill-downs against the
Parquet sink:

```python
mew.set_context("dataset.size", 1000)
mew.set_context("dataset.kind", "uniform")
# -> {"dataset": {"size": 1000, "kind": "uniform"}}
```

As an example, a DuckDB query on the resulting run data might look like this:

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

# Reporters

A reporter is a Python class with `report_context(context)` and `report_runs(runs)` methods;
with an optional `finalize()` API.
The C++ runner calls them in the main thread with the GIL held.

## Built-ins

{class}`~mew.RichReporter`
: Streams one row per Run as a rich-formatted table. The default for
  `mew run` when no `-o` is given. Optional columns: `Peak Mem` /
  `Total Alloc` (via `show_memory=True`) and `Samples` / `Hottest Frame`
  (via `show_cpu=True`). The CLI exposes those options via
  `--profile-memory` / `--sample`.

{class}`~mew.JSONReporter`
: Emits a single `{"context": ..., "benchmarks": [...]}` document, shaped
  like Google Benchmark's own JSON. Buffers in memory and writes on
  `finalize()`. Pass `output=Path(...)`, a text stream, or omit for
  stdout.

{class}`~mew.ParquetReporter`
: One row per Run, static schema, user context flattened. Requires
  `pyarrow` (`pip install 'mew[dev]'` or `pip install pyarrow`). Counters
  are a `MAP<string, double>`. The user-defined context goes into a JSON
  string column named `custom` — query with e.g. DuckDB's `json_extract`;
  session identity lands as `session_id` / `session_tag` string columns
  (see [](context.md)).

{class}`~mew.Fanout`
: Broadcasts each callback to a list of reporters. Used internally by
  `mew run` when multiple `-o` sinks are supplied. `report_context()`
  returns `all(...)` — Google Benchmark halts when any reporter returns
  `False`, so the strictest sub-reporter wins.

## Choosing a sink

| Use case                                  | Recommended sink                |
| ----------------------------------------- | ------------------------------- |
| Interactive iteration on a laptop         | `RichReporter` (default)        |
| Capture a baseline for `mew compare`      | `JSONReporter` (`-o b.json`)    |
| Long-running results, SQL analytics       | `ParquetReporter` (`-o b.pq`)   |
| Console output + persisted artifact       | `-o -` plus `-o file.{json,pq}` |

## Custom reporters

Implementing the protocol is mechanical:

```python
from typing import Any
from mew import Run


class MetricsExporter:
    def __init__(self, sink):
        self._sink = sink
        self._context: dict[str, Any] = {}

    def report_context(self, context: dict[str, Any]) -> bool:
        self._context = context
        return True

    def report_runs(self, runs: list[Run]) -> None:
        for r in runs:
            self._sink.push(name=r.benchmark_name(), value=r.adjusted_real_time())

    def finalize(self) -> None:
        self._sink.flush()
```

Pass it directly to {func}`mew.run`:

```python
from mew import REGISTRY, run

run(REGISTRY.all(), reporter=MetricsExporter(sink))
```

## DuckDB recipes

```sql
-- 95th percentile real_time per benchmark
SELECT name, quantile_cont(real_time, 0.95) AS p95
FROM 'results.parquet'
GROUP BY name
ORDER BY p95 DESC;

-- Custom context drill-down
SELECT name,
       json_extract(custom, '$.dataset.size') AS size,
       avg(real_time) AS mean_time
FROM 'results.parquet'
GROUP BY name, size;
```

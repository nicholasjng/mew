# Reporters

A reporter is a Python class with `report_context(context)` and `report_runs(runs)` methods, plus an optional `finalize()`.
Calls arrive on the main thread, so a reporter needs no locking of its own.

`report_runs` receives a list of {class}`~mew.RunRow` dicts, one per completed
run, with any `memory` / `cpu_profile` blocks already attached. Rows are plain
dicts (`row["real_time"]`, `row.get("memory")`), so one reporter serves ordinary
runs and externally merged rows alike.

## Built-ins

{class}`~mew.RichReporter`
: Streams one row per run as a formatted, colorized table. The default for
  `mew run` when no `-o` is given. Optional columns: `Peak Mem`
  (via `show_memory=True`) and `Samples` / `Hottest Frame`
  (via `show_cpu=True`). The CLI exposes those options via
  `--profile-memory` / `--sample`.

{class}`~mew.JSONReporter`
: Emits a single `{"context": ..., "benchmarks": [...]}` document, shaped
  like Google Benchmark's own JSON. Buffers in memory and writes on
  `finalize()`. Pass `output=Path(...)`, a text stream, or omit for
  stdout.

{class}`~mew.Fanout`
: Broadcasts each callback to a list of reporters; what `mew run` uses when
  several `-o` sinks are given. A run halts if any sub-reporter returns
  `False` from `report_context()`, so the strictest one wins.

## Choosing a sink

| Use case                                  | Recommended sink                   |
| ----------------------------------------- | ---------------------------------- |
| Interactive iteration on a laptop         | `RichReporter` (default)           |
| Capture a baseline for `mew compare`      | `JSONReporter` (`-o b.json`)       |
| Growing archive, SQL analytics            | `JSONLReporter` (`-o b.jsonl[.gz]`) |
| Console output + persisted artifact       | `-o -` plus `-o file.{json,jsonl}` |

JSONL rows are self-contained — each carries its session identity and `custom`
context — so an archive is plain NDJSON that analytical tools read directly.
`.jsonl.gz` compresses it; `--append` adds each run as a new gzip member, so
appends stay cheap.

## Custom reporters

```python
from typing import Any
from mew import RunRow


class MetricsExporter:
    def __init__(self, sink):
        self._sink = sink
        self._context: dict[str, Any] = {}

    def report_context(self, context: dict[str, Any]) -> bool:
        self._context = context
        return True

    def report_runs(self, runs: list[RunRow]) -> None:
        for row in runs:
            self._sink.push(name=row["name"], value=row["real_time"])

    def finalize(self) -> None:
        self._sink.flush()
```

Each `row` is a {class}`~mew.RunRow`: the base keys (`name`, `real_time`,
`cpu_time`, `iterations`, `time_unit`, `label`, `counters`, …) are always
present; `custom`, `memory`, and `cpu_profile` appear only when relevant
(under {func}`mew.set_context` / `--profile-memory` / `--sample`).

Pass it directly to {func}`mew.run`:

```python
from mew import REGISTRY, run

run(REGISTRY.all(), reporter=MetricsExporter(sink))
```

## SQL and dataframe recipes

DuckDB, pandas, and polars read the JSONL archive directly: nested blocks
(`custom`, `memory`) arrive as structs, and gzip is handled transparently
(polars: decompress first).

```sql
-- 95th percentile real_time per benchmark (also works on 'results.jsonl.gz')
SELECT name, quantile_cont(real_time, 0.95) AS p95
FROM 'results.jsonl'
GROUP BY name
ORDER BY p95 DESC;

-- Custom context drill-down: nested blocks are structs, not JSON strings
SELECT name, custom.dataset.size AS size, avg(real_time) AS mean_time
FROM 'results.jsonl'
GROUP BY name, size;
```

```python
import pandas as pd

df = pd.read_json("results.jsonl.gz", lines=True)  # compression inferred

import polars as pl

df = pl.read_ndjson("results.jsonl")
```

Convert to Parquet after the fact, one file or many:

```sql
COPY (FROM 'results.jsonl') TO 'results.parquet';
COPY (FROM read_json_auto(['a.jsonl', 'b.jsonl.gz'], union_by_name=true))
  TO 'archive.parquet';
```

# Reporters

A reporter is a Python class with `report_context(context)` and `report_runs(runs)` methods, plus an optional `finalize()`.
The C++ runner calls them in the main thread with the GIL held.

`report_runs` receives a list of {class}`~mew.RunRow` dicts, each one a
completed run projected from the C++ `Run` at the binding boundary, with any
`memory` / `cpu_profile` blocks already attached. Reporters read dict keys
(`row["real_time"]`, `row.get("memory")`), so the same reporter serves both
in-process runs and the `mew run --variant` merge (which only ever has dicts,
never a live `Run`).

## Built-ins

{class}`~mew.RichReporter`
: Streams one row per Run as a formatted, colorized table. The default for
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
: Broadcasts each callback to a list of reporters. Used internally by
  `mew run` when multiple `-o` sinks are supplied. `report_context()`
  returns `all(...)`: Google Benchmark halts when any reporter returns
  `False`, so the strictest sub-reporter wins.

## Choosing a sink

| Use case                                  | Recommended sink                   |
| ----------------------------------------- | ---------------------------------- |
| Interactive iteration on a laptop         | `RichReporter` (default)           |
| Capture a baseline for `mew compare`      | `JSONReporter` (`-o b.json`)       |
| Growing archive, SQL analytics            | `JSONLReporter` (`-o b.jsonl[.gz]`) |
| Console output + persisted artifact       | `-o -` plus `-o file.{json,jsonl}` |

JSONL rows are self-contained (each carries its session identity and `custom`
context), so an archive is plain NDJSON that analytical tools read directly —
no export step, no extra dependency. `.jsonl.gz` compresses the archive;
`--append` adds each run as a new gzip member, so appends stay cheap.

## Custom reporters

The protocol is mechanical to implement:

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
present; `variant`, `custom`, `memory`, and `cpu_profile` appear only when
relevant (under `--variant` / `--profile-memory` / `--sample`).

Pass it directly to {func}`mew.run`:

```python
from mew import REGISTRY, run

run(REGISTRY.all(), reporter=MetricsExporter(sink))
```

## SQL and dataframe recipes

The JSONL archive is directly intelligible to DuckDB, pandas, and polars —
nested blocks (`custom`, `memory`) arrive as structs, and gzip is handled
transparently (polars: decompress first).

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
df = pd.read_json("results.jsonl.gz", lines=True)   # compression inferred

import polars as pl
df = pl.read_ndjson("results.jsonl")
```

Need Parquet for a data-lake handoff? Convert after the fact — one line,
including many files at once:

```sql
COPY (FROM 'results.jsonl') TO 'results.parquet';
COPY (FROM read_json_auto(['a.jsonl', 'b.jsonl.gz'], union_by_name=true))
  TO 'archive.parquet';
```

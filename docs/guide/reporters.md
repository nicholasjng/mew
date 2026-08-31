# Reporters

A reporter implements `report_context(context)` and `report_runs(runs)`, with an
optional `finalize()`. Calls arrive on the main thread. Rows are plain
{class}`~mew.BenchmarkResult` dictionaries.

## Built-ins

{class}`~mew.RichReporter`
: Streams one row per run as a formatted, colorized table. The default for
  `mew run` when no `-o` is given. Optional columns: `Peak Mem`
  (via `show_memory=True`) and `Samples` / `Hottest Frame`
  (via `show_cpu=True`). The CLI exposes those options via
  `--profile-memory` / `--sample`.

{class}`~mew.JSONReporter`
: Streams one `{"context": ..., "benchmarks": [...]}` document. It becomes
  valid JSON at `finalize()`. Pass a path, text stream, or omit for stdout.

{class}`~mew.JSONLReporter`
: Streams self-contained NDJSON rows. Use it for append-only or
  interruption-tolerant archives.

{class}`~mew.Fanout`
: Broadcasts callbacks to several reporters. A child exception stops the run.

## Reading results back

{func}`mew.compare.read_results` and {func}`mew.compare.read_sessions` accept
JSON, JSONL, and gzip-compressed results.

```python
from mew.compare import read_sessions

for session in read_sessions("results.jsonl"):
    print(session.tag or session.session_id[:8], session.provenance.get("engine"))
    for name, sample in session.samples.items():
        print(f"  {name}  {sample.value:.1f} {sample.time_unit}  cv={sample.cv:.3f}")
```

`read_sessions` drops aggregate and skipped rows, canonicalizes case names, and
reduces repetitions to {class}`~mew.compare.Sample` objects. Pass `metric=` for
a measurement other than `real_time`.

`read_results` returns every stored row without filtering.

## Choosing a sink

| Use case                                  | Recommended sink                   |
| ----------------------------------------- | ---------------------------------- |
| Interactive iteration on a laptop         | `RichReporter` (default)           |
| Capture a baseline for `mew compare`      | `JSONReporter` (`-o b.json`)       |
| Growing archive, SQL analytics            | `JSONLReporter` (`-o b.jsonl[.gz]`) |
| Console output + persisted artifact       | `-o -` plus `-o file.{json,jsonl}` |

Each JSONL row carries `session` and `context`. `.jsonl.gz` compresses the archive;
`--append` adds a gzip member without rewriting earlier data.

## Custom reporters

```python
from typing import Any
from mew import BenchmarkResult


class MetricsExporter:
    def __init__(self, sink):
        self._sink = sink
        self._context: dict[str, Any] = {}

    def report_context(self, context: dict[str, Any]) -> None:
        self._context = context

    def report_runs(self, runs: list[BenchmarkResult]) -> None:
        for row in runs:
            self._sink.push(name=row["name"], value=row["real_time"])

    def finalize(self) -> None:
        self._sink.flush()
```

Base measurement keys are always present; `session`, `context`, `memory`, and
`cpu_profile` are conditional.

Pass it directly to {func}`mew.run`:

```python
from mew import REGISTRY, run

run(REGISTRY.all(), reporter=MetricsExporter(sink))
```

## SQL and dataframe recipes

DuckDB, pandas, and polars read the JSONL archive directly: nested blocks
(`context`, `memory`) arrive as structs, and gzip is handled transparently
(polars: decompress first).

```sql
-- 95th percentile real_time per benchmark (also works on 'results.jsonl.gz')
SELECT name, quantile_cont(real_time, 0.95) AS p95
FROM 'results.jsonl'
GROUP BY name
ORDER BY p95 DESC;

-- Custom context drill-down: nested blocks are structs, not JSON strings
SELECT name, context.dataset.size AS size, avg(real_time) AS mean_time
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

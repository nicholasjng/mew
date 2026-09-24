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
: Streams one `{"session": ..., "context": ..., "benchmarks": [...]}` document
  whose rows are the same self-contained objects JSONL writes. It becomes valid
  JSON at `finalize()`. Pass a path, text stream, or omit for stdout.

{class}`~mew.JSONLReporter`
: Streams self-contained NDJSON rows. Use it for append-only or
  interruption-tolerant archives.

{class}`~mew.Fanout`
: Broadcasts callbacks to several reporters. A child exception stops the run.

## Reading results back

{func}`mew.compare.read_results` returns every stored row of a JSON, JSONL, or
gzip-compressed result file. {func}`mew.compare.session_summaries`
lists the sessions a file holds, newest first (what `mew sessions` prints).

```python
from statistics import median

from mew.compare import read_results, session_summaries

newest = session_summaries("results.jsonl")[0]
rows = [
    r for r in read_results("results.jsonl")
    if r["session"]["id"] == newest.id and not r["aggregate_name"] and not r["skipped"]
]
for name in sorted({r["benchmark"] for r in rows}):
    print(name, median(r["real_time"] for r in rows if r["benchmark"] == name))
```

For anything beyond a few lines of Python, query the file directly; see the
recipes below.

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

Base measurement keys are always present; `session`, `context`, `benchmark`,
`memory`, and `cpu_profile` are conditional. `benchmark` is the benchmark as
mew addresses it (`file.py::func[label]`, plus `/threads:N` for threaded runs),
shared by all rows of one benchmark, so queries group on it without parsing
`name`. Names never contain `[`, so `split_part(benchmark, '[', 1)` recovers
the function:

```sql
SELECT benchmark, median(real_time) FROM 'results.jsonl' GROUP BY benchmark;
SELECT split_part(benchmark, '[', 1) AS func, avg(real_time)
FROM 'results.jsonl' GROUP BY func;
```

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

Pull one session out of a large archive into a file `mew compare` reads
directly. DuckDB infers `session.id` as a UUID and `session.date` as a
timestamp, hence the casts:

```sql
COPY (SELECT * FROM 'results.jsonl.gz' WHERE session.tag = 'before')
  TO 'before.jsonl' (FORMAT JSON);
COPY (SELECT * FROM 'results.jsonl.gz'
      WHERE starts_with(session.id::VARCHAR, '01a0c787'))
  TO 'run.jsonl' (FORMAT JSON);
```

Convert to Parquet after the fact, one file or many:

```sql
COPY (FROM 'results.jsonl') TO 'results.parquet';
COPY (FROM read_json_auto(['a.jsonl', 'b.jsonl.gz'], union_by_name=true))
  TO 'archive.parquet';
```

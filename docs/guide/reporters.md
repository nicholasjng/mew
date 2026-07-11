# Reporters

A reporter is a Python class with `report_context(context)` and `report_runs(runs)` methods, plus an optional `finalize()`.
Calls arrive on the main thread, so a reporter needs no locking of its own.

`report_runs` receives a list of {class}`~mew.BenchmarkResult` dicts, one per completed
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
  several `-o` sinks are given. A sub-reporter that raises halts the run, so the
  strictest one wins.

## Reading results back

`mew` writes result files; {func}`mew.compare.read_results` and
{func}`mew.compare.read_sessions` read them. Both accept `.json`, `.jsonl` and
either gzipped. They live in `mew.compare` rather than the top-level namespace:
the top level is for *writing* benchmarks, and this is analysis machinery you
reach for in a separate script.

```python
from mew.compare import read_sessions

for session in read_sessions("results.jsonl"):
    print(session.tag or session.session_id[:8], session.provenance.get("engine"))
    for name, sample in session.samples.items():
        print(f"  {name}  {sample.value:.1f} {sample.time_unit}  cv={sample.cv:.3f}")
```

`read_sessions` does what a script would otherwise reimplement: it drops Google
Benchmark's aggregate rows and skipped rows, canonicalizes `bench.py::f/case:0`
to `bench.py::f[n=10]`, groups repetitions, and reduces each group to one
{class}`~mew.compare.Sample` carrying the center, the stddev and the raw
`values`. Pass `metric=` for a measurement other than `real_time`.

`read_results` is the raw view — every row as stored, nothing filtered — for
feeding a dataframe or doing your own reduction.

## Choosing a sink

| Use case                                  | Recommended sink                   |
| ----------------------------------------- | ---------------------------------- |
| Interactive iteration on a laptop         | `RichReporter` (default)           |
| Capture a baseline for `mew compare`      | `JSONReporter` (`-o b.json`)       |
| Growing archive, SQL analytics            | `JSONLReporter` (`-o b.jsonl[.gz]`) |
| Console output + persisted artifact       | `-o -` plus `-o file.{json,jsonl}` |

JSONL rows are self-contained — each carries its `session` identity and `context`
context — so an archive is plain NDJSON that analytical tools read directly.
`.jsonl.gz` compresses it; `--append` adds each run as a new gzip member, so
appends stay cheap.

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

Each `row` is a {class}`~mew.BenchmarkResult`: the base keys (`name`, `real_time`,
`cpu_time`, `iterations`, `time_unit`, `label`, `counters`, …) are always
present; `session`, `context`, `memory`, and `cpu_profile` appear only when relevant
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

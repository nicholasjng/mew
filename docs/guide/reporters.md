# Reporters

A reporter implements `report_context(context)` and `report_results(runs)`, with an
optional `finalize()`. Calls arrive on the main thread. Rows are plain
{class}`~mew.BenchmarkResult` dictionaries.

## Built-ins

{class}`~mew.RichReporter`
: Streams one row per run as a formatted, colorized table. The default for
  `mew run` when no `-o` is given, and for `mew.run()` without a `reporter`.
  It is as wide as `COLUMNS`, or else the terminal found through stdout, stderr
  or the controlling terminal, or else 80 columns; CI has no terminal, so set
  `COLUMNS` there for a wider log. The name column is as wide as its longest
  name: `mew run` and `mew.run()` pass it as `name_width`. Times are scaled to
  a readable unit, and `show_memory=True` adds a `Peak Mem` column. Below a
  row, an indented line lists what has no column: a label the benchmark set,
  its counters and, with `show_cpu=True`, its sample count, hottest function
  and, on free-threaded 3.15+, hottest bytecode instruction. The CLI sets them
  for `--memory-profiler` and `--cpu-profiler`. When the run ends, a second table sums
  up each repeated benchmark, as `mew compare` reduces it; with
  `show_repetitions=False` (`mew run --summary-only`), repeated benchmarks
  appear only there.

{class}`~mew.JSONReporter`
: Streams one `{"run": ..., "context": ..., "benchmarks": [...]}` document
  whose rows are the same self-contained objects JSONL writes. It becomes valid
  JSON at `finalize()`. Pass a path, text stream, or omit for stdout.

{class}`~mew.JSONLReporter`
: Streams self-contained NDJSON rows. Use it for append-only or
  interruption-tolerant archives.

{class}`~mew.Fanout`
: Broadcasts callbacks to several reporters. A child exception stops the run.

## Reading results back

{func}`mew.compare.read_results` returns every stored row of a JSON, JSONL, or
gzip-compressed result file. {func}`mew.compare.run_summaries`
lists the runs a file holds, newest first (what `mew log` prints).

```python
from statistics import median

from mew.compare import read_results, run_summaries

newest = run_summaries("results.jsonl")[0]
rows = [
    r for r in read_results("results.jsonl")
    if r["run"]["id"] == newest.id and r["skip"] is None
]
for name in sorted({r["name"] for r in rows}):
    print(name, median(r["real_time"] for r in rows if r["name"] == name))
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

Each JSONL row carries `run` and `context`. `.jsonl.gz` compresses the archive;
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

    def report_results(self, runs: list[BenchmarkResult]) -> None:
        for row in runs:
            self._sink.push(name=row["name"], value=row["real_time"])

    def finalize(self) -> None:
        self._sink.flush()
```

A row's keys are listed in {class}`mew.BenchmarkResult`; `memory` and
`cpu_profile` appear only when a profiler ran. `name` is the benchmark as mew
addresses it (`file.py::func[label]`, plus `/threads:N` for threaded runs),
shared by all rows of one benchmark. Function names never contain `[`, so
`split_part(name, '[', 1)` recovers the function, and a family case's
parameters are in `params`. Times are in nanoseconds, and a skipped row has
`skip`, `{"reason": "error" | "skipped", "message": ...}`, instead of `null`:

```sql
SELECT name, median(real_time) FROM 'results.jsonl' WHERE skip IS NULL GROUP BY name;
SELECT split_part(name, '[', 1) AS func, params.n, avg(real_time)
FROM 'results.jsonl' GROUP BY func, params.n;
```

Result files add `schema_version` to every row: the result format, `1`. mew
reads only its own format and rejects rows of another with a message, so
re-run old benchmarks rather than converting their files. Parameters and
context values that JSON can't represent are stored as their type's qualified
name, such as `"torch.Tensor"`, never as a `str()` of the object. Floats keep
NaN and infinity, written as `NaN` and `Infinity` as Python's `json` does:
DuckDB and Python read them back, but `jq` turns them into `null` and the
largest finite double.

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

-- One counter per row: `counters` is a list of {name, value, format} records
SELECT name, c.value AS bytes_per_second
FROM 'results.jsonl', unnest(counters) AS t(c)
WHERE c.name = 'bytes_per_second';
```

```python
import pandas as pd

df = pd.read_json("results.jsonl.gz", lines=True)  # compression inferred

import polars as pl

df = pl.read_ndjson("results.jsonl")
```

Pull one run out of a large archive into a file `mew compare` reads
directly. DuckDB infers `run.id` as a UUID and `run.date` as a
timestamp, hence the casts:

```sql
COPY (SELECT * FROM 'results.jsonl.gz' WHERE run.name = 'before')
  TO 'before.jsonl' (FORMAT JSON);
COPY (SELECT * FROM 'results.jsonl.gz'
      WHERE starts_with(run.id::VARCHAR, '01a0c787'))
  TO 'run.jsonl' (FORMAT JSON);
```

Convert to Parquet after the fact, one file or many:

```sql
COPY (FROM 'results.jsonl') TO 'results.parquet';
COPY (FROM read_json_auto(['a.jsonl', 'b.jsonl.gz'], union_by_name=true))
  TO 'archive.parquet';
```

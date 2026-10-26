# Analyze saved results

Use an appendable archive when you want to track many runs or ask more than "this file against that one".
JSONL stores a complete result row on each line, including its run and context:

```console
$ mew run --run-name nightly --append -o history.jsonl.gz
```

`mew compare` treats an archive as one sample and pools every run in it, or every run [`--where`](../guide/comparing.md#select-rows) selects.
For anything else, query the archive directly, or extract runs into files `mew compare` reads.

The [result format](../reference/results.md) documents every field a row carries.

## Read one run in Python

```python
from statistics import median

from mew.compare import read_results, run_summaries

newest = run_summaries("history.jsonl.gz")[0]
rows = [
    row
    for row in read_results("history.jsonl.gz", where={"run.id": newest.id})
    if row["skip"] is None
]
for name in sorted({row["name"] for row in rows}):
    print(name, median(row["real_time"] for row in rows if row["name"] == name), "ns")
```

`read_results()` reads JSON, JSONL, and compressed JSONL, and returns rows as stored, including skipped rows.
Its `where` takes the same conditions as [`--where`](../guide/comparing.md#select-rows), by path.
`run_summaries()` lists the runs newest first, as `mew log` prints them, and takes `where` too.

## Summarize a history with SQL

[DuckDB](https://duckdb.org/docs/stable/data/json/overview.html) queries a JSONL archive directly, including gzip-compressed files.
Install it separately.
Nested blocks arrive as structs.

For the [JAX history recipe](git-history.md), summarize repetitions per commit and run:

```sql
SELECT
    context.vcs.commit AS commit,
    run.id AS run_id,
    min(run.date) AS measured_at,
    name,
    median(real_time) AS median_ns,
    count(*) AS repetitions
FROM 'history.jsonl'
WHERE skip IS NULL
GROUP BY commit, run_id, name
ORDER BY measured_at;
```

Keeping `run_id` in the grouping makes repeated measurements of a commit visible.
Only pool those runs after checking that they used the same environment and workload.
Swap `median` for `quantile_cont(real_time, 0.95)` or any other aggregate: `mew compare` always uses the median.

Counters are a list of `{name, value, format}` records:

```sql
SELECT run.id, name, c.value AS bytes_per_second
FROM 'results.jsonl', unnest(counters) AS t(c)
WHERE skip IS NULL AND c.name = 'bytes_per_second';
```

Context and parameters are available the same way, such as `context.dataset.size` or `params.n`.
Function names never contain `[`, so `split_part(name, '[', 1)` recovers the family of a parametrized case.

(select-runs)=
## Select runs

`mew log --where` and `mew show --where` narrow an archive to the runs you name, and `mew compare --where` pools only those runs in every file.
To compare two runs of one archive, write each to its own file with `read_results()`:

```python
import json

from mew.compare import read_results

for name in ("before", "after"):
    with open(f"{name}.jsonl", "w") as f:
        for row in read_results("history.jsonl.gz", where={"run.name": name}):
            f.write(json.dumps(row) + "\n")
```

Then run `mew compare before.jsonl after.jsonl`.
A glob selects a run by its id's prefix, as `mew log` shows it: `where={"run.id": "01a0c787*"}`.

(extract-a-run-or-convert-to-parquet)=
## Extract a run or convert to Parquet

Export runs to a JSONL file that `mew compare` reads:

```sql
COPY (SELECT * FROM 'history.jsonl.gz' WHERE run.name = 'before')
    TO 'before.jsonl' (FORMAT JSON);
COPY (SELECT * FROM 'history.jsonl.gz' WHERE starts_with(run.id::VARCHAR, '01a0c787'))
    TO 'run.jsonl' (FORMAT JSON);
```

DuckDB infers `run.id` as a UUID, which is why the cast is needed.

Convert one archive or several to Parquet for later queries:

```sql
COPY (FROM 'history.jsonl') TO 'history.parquet';
COPY (FROM read_json_auto(['a.jsonl', 'b.jsonl.gz'], union_by_name=true)) TO 'archive.parquet';
```

## Load a dataframe

Install the reader you use separately:

```python
import pandas as pd

df = pd.read_json("history.jsonl.gz", lines=True)
```

```python
import polars as pl

df = pl.read_ndjson("history.jsonl")
```

Decompress the archive first when using Polars.

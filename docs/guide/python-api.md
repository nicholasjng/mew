# Using mew from Python

The decorators register benchmarks.
{func}`mew.run` executes them and sends measurements to reporters.
Import benchmark modules yourself when using Python.
The CLI handles file discovery.

## Register and run

Save this as `bench_sort.py` and run `python bench_sort.py`:

```python
from pathlib import Path

import mew


@mew.parametrize([{"n": 100}, {"n": 1000}], tags="sort")
def bench_sorted(state: mew.State, n: int) -> None:
    data = list(range(n, 0, -1))
    for _ in state:
        sorted(data)


if __name__ == "__main__":
    count = mew.run(
        reporter=[
            mew.RichReporter(),
            mew.JSONReporter(output=Path("results.json")),
        ],
        min_time="500ms",
        repetitions=10,
        run_name="baseline",
    )
    if count == 0:
        raise SystemExit("No benchmarks selected")
```

Importing the module adds its benchmarks to {data}`mew.REGISTRY`.
`run()` defaults to all registered entries and returns the number of benchmarks run.
Inspect result rows for skips when automating a run.
The main guard lets the CLI import the file without executing its `run()` call.
`[tool.mew]` configuration applies to the CLI.
Python calls use their arguments and registered options.

## Inputs, names, and tags

Use `@mew.benchmark` for one case and `@mew.parametrize` for a family of cases, one per argument dictionary as above.
{func}`mew.grid` builds the dictionaries for every combination of independent inputs:

```python
@mew.parametrize(mew.grid(n=[100, 1000], reverse=[False, True]))
def bench_sort_cases(state: mew.State, n: int, reverse: bool) -> None:
    data = list(range(n, 0, -1))
    for _ in state:
        sorted(data, reverse=reverse)
```

The last axis varies fastest.
A grid is a plain list, so join grids with `+` or filter one with a comprehension when only some combinations make sense.
Options such as `threads` go to `@mew.parametrize`, never to `grid`.

Apply one registration decorator per function.
Default names look like `bench_sort.py::bench_sorted[n=100]`, relative to the project root.
Use `name="sort"` for a stable custom name and `ids=["small", "large"]` for explicit case labels.
Labels must be unique, with one per case.
Keep names and labels stable when tracking history.
`tags="sort"` or `tags=("sort", "hot-path")` adds selection tags.
Each row records its case's arguments in `params`.

Select entries with {meth}`mew.Registry.filter`:

```python
entries = mew.REGISTRY.filter(pattern="bench_sorted[n=1000]", literal=True)
mew.run(entries, repetitions=5)
```

Without `literal=True`, the pattern is a regular expression searched within each name.
Add `tags=["sort"]` to narrow the selection by tag.
Multiple tags use OR.
Import modules once per process: clearing the registry does not cause a subsequent import of the same module to execute its decorators again.

## Options and metadata

Decorator options override defaults passed to `run()`.
The exception is `run(min_time="100x")`, which forces an iteration count even for a benchmark with a time-based `min_time`.
A benchmark's own `iterations` still wins.
See {class}`mew.api.BenchmarkOptions` for the full option list and [run length](writing-benchmarks.md#run-length) for timing choices.

Pass `context` to record what you measured:

```python
mew.run(
    run_name="before",
    context={**mew.vcs_context(), "dataset": {"name": "uniform", "size": 1000}},
)
```

Every row stores the context next to mew's machine and interpreter fields, and your keys win over mew's.
Nested mappings and lists are kept.
A value JSON cannot hold is stored as its type's qualified name.
Pass a checkout `Path` to `vcs_context()` when the code under test lives elsewhere.
See the [result format](../reference/results.md) for the stored fields and the [setup file](../reference/configuration.md#setup-file) for the CLI's `context()` hook.

## Receive or read results

A custom reporter can keep rows in memory:

```python
class Collector:
    def __init__(self):
        self.rows: list[mew.BenchmarkResult] = []

    def report_context(self, context):
        pass

    def report_results(self, results):
        self.rows.extend(results)


collector = Collector()
mew.run(reporter=collector)
for row in collector.rows:
    if row["skip"] is None:
        print(row["name"], row["real_time"], "ns")
```

Times are nanoseconds.
Group repetitions by `name`, which includes case labels and thread counts.
A skipped row has `skip`, `{"reason": ..., "message": ...}`, instead of `None`.
For saved results, use {func}`mew.compare.read_results`.
The [archive recipe](../recipes/analyzing-results.md) shows run selection and statistics.
The [reporter reference](../reference/api/reporters.md) documents the callbacks.

See [compare from Python](comparing.md#compare-from-python) to gate saved results and [profiling](../howto/profiling.md#from-python) to collect CPU or memory profiles.

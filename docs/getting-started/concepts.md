# Concepts

## Benchmarks and families

A **benchmark** is a single function decorated with `@mew.benchmark`.
A **benchmark family** is a set of benchmarks produced by `@mew.parametrize` or `@mew.product`, where the same function body is registered once per parameter combination.
The variant label is appended to the registration name in brackets: `bench_sort[n=10-algo=merge]`.

## Registration name

A benchmark's name is its identifier in result files and the target of `-k`/`--pattern` filters.
By default it follows the pytest convention:

```
benchmarks/bench_sort.py::bench_sorted[n=10]
```

You can override this name with the `name="..."` argument on the decorator.
Variant labels (`[…]`) are always appended for families.

## `State`

The `State` object passed to your function is a thin wrapper around Google Benchmark's `benchmark::State`.
The minimum contract is the iteration loop:

```python
for _ in state:
    ...  # timed
```

For sections that should be excluded from the timing (deserialization, randomization, etc.), use the `State.pause()` context manager:

```python
for _ in state:
    with state.pause():
        data = build_input()
    process(data)
```

See [](../guide/state-and-timing.md) for counters, real-vs-CPU timing, and manual iteration counts.

## Registry

Decorators add an `Entry` to a process-global `Registry`.
`mew run` discovers files, imports them, filters the registry, and hands the selected entries to the C++ runner.
You can also drive a run programmatically:

```python
from mew import REGISTRY, run, JSONReporter
run(REGISTRY.all(), reporter=JSONReporter(output=Path("out.json")))
```

## Reporters

Reporters are duck-typed objects with `report_context()` and `report_runs()` methods.
Built-ins: {class}`~mew.RichReporter`, {class}`~mew.JSONReporter`, {class}`~mew.ParquetReporter`.
Combine several with {class}`~mew.Fanout`.
See [](../guide/reporters.md).

## Context

`mew.set_context()` and `mew.update_context()` populate a process-global metadata dict that's merged into every reporter's context under the `custom` key.
Use it to stamp results with a git SHA, dataset identifiers, hardware tags — anything you'd later want to filter on.
See [](../guide/context.md).

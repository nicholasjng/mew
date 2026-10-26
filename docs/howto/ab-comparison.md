# Compare implementations in separate processes

Some comparisons cannot share an interpreter: two libraries that statically link different versions of the same native library, a GIL build against a free-threaded one, or a release build against a sanitizer build.
Run each side in its own process, write each side to its own result file, and compare the files.

## One benchmark file for both sides

Write the benchmarks once and let an environment variable pick the implementation.
Both sides then run the same code under the same names, so their rows match:

```python
# benchmarks/bench_engine.py
import importlib
import os

import mew

engine = importlib.import_module(os.environ["ENGINE"])  # "alpha" or "beta"


@mew.benchmark
def bench_scan(state: mew.State) -> None:
    table = engine.load("fixtures/orders.parquet")
    for _ in state:
        engine.scan(table)
```

Each process imports only its own implementation.
If the two APIs differ, map them onto one in the file, for example with a dict of small adapter functions.
Input construction stays outside the timing loop, and so do any correctness checks.

## Alternate the sides

Run the sides in turn, appending each round to that side's file:

```bash
for i in 1 2 3 4 5; do
    ENGINE=alpha mew run --repetitions 4 --context engine="alpha 1.5.3" --append -o alpha.jsonl
    ENGINE=beta  mew run --repetitions 4 --context engine="beta 2.0.0"  --append -o beta.jsonl
done
mew compare --show-context engine alpha.jsonl beta.jsonl
```

Alternating spreads thermal and background-load drift across both implementations.
A file is one sample, so each file pools its five runs as 20 repetitions.
Start from fresh files for each experiment, since the runs already in a file pool with the new ones.
`--context` records what each side is, and `--show-context engine` adds it to the context table above the comparison, next to each file's run count and newest run:

```text
      │ runs │ date                │ engine
────────────────────────────────────────────────
alpha │ 5    │ 2026-11-09 14:44:34 │ alpha 1.5.3
beta  │ 5    │ 2026-11-09 14:44:35 │ beta 2.0.0

Comparison of benchmarks/bench_engine.py (real_time, baseline alpha)
Benchmark  │           alpha │          beta │                 Δ% │ speedup
───────────────────────────────────────────────────────────────────────────
bench_scan │ 558.57 ns ±0.3% │ 2.05 us ±1.5% │ +267.67% (signif.) │  ×0.272
beta against alpha: 1 slower, 0 faster, 0 not significantly different
```

## Compare Python builds

Install mew and the benchmark's dependencies in both environments, and run the same benchmark file in each:

```bash
for i in 1 2 3 4 5; do
    .venv-gil/bin/mew run benchmarks/bench_engine.py --repetitions 4 --append -o gil.jsonl
    .venv-ft/bin/mew run benchmarks/bench_engine.py --repetitions 4 --append -o ft.jsonl
done
mew compare gil.jsonl ft.jsonl
```

Every row records the Python version, free-threading build, and runtime GIL state in `context.python`, so the context lines label the columns.
An imported extension can enable the GIL on a free-threaded build, so check `gil_enabled` before interpreting the result.

## Profile both sides

The profiling flags apply per invocation, so give each side its own report directory:

```console
$ ENGINE=alpha mew run --memory-profiler memray --profile-dir profiles.alpha -o alpha.json
$ ENGINE=beta  mew run --memory-profiler memray --profile-dir profiles.beta  -o beta.json
$ mew compare --metric memory.allocations_per_iteration alpha.json beta.json
```

Compare per-iteration allocations, since a faster implementation runs more iterations.
[Track allocations](profiling.md#track-allocations) shows how to find where they come from.

# A/B comparison across processes

Some comparisons cannot share an interpreter: two engines whose extension
modules statically link different versions of the same native library, a GIL
build against a free-threaded one, a release build against an AddressSanitizer
one. Run each side in its own process, write each to its own result file, and
compare the files.

## One file for both sides

Write the benchmarks once and let an environment variable pick the engine, so
both sides run the same code under the same names, and their rows line up:

```python
# benchmarks/bench_engine.py
import importlib
import os

import mew

engine = importlib.import_module(os.environ["ENGINE"])  # "alpha" or "beta"


@mew.benchmark
def bench_scan(state):
    for _ in state:
        engine.scan()
```

Each process imports only its own engine. If the two APIs differ, map them
onto one in the file, e.g. with a dict of small adapter functions per engine.
Record what each side is with `--context`, so the comparison documents the
version skew.

What both sides share, such as the commit they ran at, belongs in the setup
file's `context()` hook (see {doc}`context`):

```python
# benchmarks/conf.py, named by [tool.mew] setup
import mew


def context() -> dict:
    return mew.vcs_context()
```

## Run each side

```console
$ ENGINE=alpha mew run --repetitions 10 --context engine="alpha 1.5.3" -o alpha.json
$ ENGINE=beta  mew run --repetitions 10 --context engine="beta 2.0.0"  -o beta.json
$ mew compare alpha.json beta.json
```

Repetitions give each value its spread and the comparison its significance
test. Each file's context line above the table shows only the values that
differ between the files, here `engine=…`:

```console
alpha: 2026-11-09T14:44:34 engine=alpha 1.5.3
beta: 2026-11-09T14:44:35 engine=beta 2.0.0
Comparison of benchmarks/bench_engine.py (real_time, baseline alpha)
Benchmark  │           alpha │          beta │                 Δ% │ speedup
───────────────────────────────────────────────────────────────────────────
bench_scan │ 558.57 ns ±0.3% │ 2.05 us ±1.5% │ +267.67% (signif.) │  ×0.272
beta against alpha: 1 slower, 0 faster, 0 not significantly different
```

The sides run one after the other, so a change in machine load or temperature
between them shows up as a difference. Keep the machine otherwise idle, and
run the pair again if a small delta matters.

## Different interpreters

Each side can run under whatever Python it needs:

```console
$ .venv-gil/bin/mew run --repetitions 10 bench_x.py -o gil.json
$ .venv-ft/bin/mew  run --repetitions 10 bench_x.py -o ft.json
$ mew compare gil.json ft.json
```

Here both sides are the *same* benchmark file, distinguished by the
interpreter. Every row records it in `context.python` (version, free-threaded
build, whether the GIL was on), and `mew compare` shows the values that differ
on each column's context line, so the sides label themselves.

## Profiling both sides

The profiling flags work per invocation, so give each side its own artifact path:

```console
$ ENGINE=alpha mew run -o alpha.json --memory-profiler memray --profile-dir profiles.alpha/
$ ENGINE=beta  mew run -o beta.json  --memory-profiler memray --profile-dir profiles.beta/
$ mew compare --metric memory.allocations_per_iteration alpha.json beta.json
```

Use `memory.allocations_per_iteration` for cross-engine allocation comparisons:
a faster engine runs more iterations, inflating the cumulative
`total_allocations` for the same per-call work (see {doc}`profiling-memory`).

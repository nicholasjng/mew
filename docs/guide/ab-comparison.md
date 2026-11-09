# A/B comparison across processes

Some comparisons cannot share an interpreter: two engines whose extension
modules statically link different versions of the same native library, a GIL
build against a free-threaded one, a release build against an AddressSanitizer
one. Run each side in its own process and merge the results into one file.

mew has no orchestrator for this; a shell loop does it, and can use a different
interpreter per side.

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

## Run them interleaved

Write each side to its own file, a run per round with `--append`, and pool
each file's runs with `@*`:

```console
$ for i in 1 2 3 4 5; do
>   ENGINE=alpha mew run --context engine="alpha 1.5.3" --append -o alpha.jsonl
>   ENGINE=beta  mew run --context engine="beta 2.0.0"  --append -o beta.jsonl
> done
$ mew compare 'alpha.jsonl@*' 'beta.jsonl@*'
```

Interleave the suites (A B A B …, not AAAAA BBBBB) to avoid bias from thermal
and load drift. Each file's context line above the table shows only the values
that differ between the files, here `engine=…`.

```console
alpha@*: 5 runs, newest 2026-11-09T09:10:36 engine=alpha 1.5.3
beta@*: 5 runs, newest 2026-11-09T09:10:36 engine=beta 2.0.0
Comparison (real_time, baseline alpha@*)
Benchmark │         alpha@* │          beta@* │                Δ% │ speedup
───────────────────────────────────────────────────────────────────────────
scan      │ 503.63 ns ±1.6% │ 948.33 ns ±1.9% │ +88.30% (signif.) │  ×0.531
beta@* against alpha@*: 1 slower, 0 faster, 0 not significantly different
```

To keep both sides in one archive instead, give each run a `--run-name` and
address them as `results.jsonl@alpha results.jsonl@beta`; see
[](regressions.md#comparing-runs-in-one-file).

## Different interpreters

Each side can run under whatever Python it needs:

```console
$ for i in 1 2 3; do
>   .venv-gil/bin/mew run bench_x.py --append -o gil.jsonl
>   .venv-ft/bin/mew  run bench_x.py --append -o ft.jsonl
> done
$ mew compare 'gil.jsonl@*' 'ft.jsonl@*'
```

Here both sides are the *same* benchmark file, distinguished by the
interpreter. Every row records it in `context.python` (version, free-threaded
build, whether the GIL was on), and `mew compare` shows the values that differ
on each column's context line, so the sides label themselves.

## Profiling both sides

The profiling flags work per invocation, so give each side its own artifact path:

```console
$ ENGINE=alpha mew run -o alpha.jsonl --memory-profiler memray --profile-dir profiles.alpha/
$ ENGINE=beta  mew run -o beta.jsonl  --memory-profiler memray --profile-dir profiles.beta/
$ mew compare --metric memory.allocations_per_iteration alpha.jsonl beta.jsonl
```

Use `memory.allocations_per_iteration` for cross-engine allocation comparisons:
a faster engine runs more iterations, inflating the cumulative
`total_allocations` for the same per-call work (see {doc}`profiling-memory`).

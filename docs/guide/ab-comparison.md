# A/B comparison across processes

Some comparisons cannot share an interpreter: two engines whose extension
modules statically link different versions of the same native library, a GIL
build against a free-threaded one, a release build against an AddressSanitizer
one. Run each side in its own process and merge the results into one file.

mew has no orchestrator for this. A shell loop does it in five lines, and the
loop is more capable than a built-in flag would be — it can use a *different
interpreter* per side, which is the whole point of the exercise.

## Tag each suite

Give each side a `custom` value naming it. This is what `compare` pivots on, and
it records the version skew in the output.

```python
# bench_alpha.py
import mew

mew.set_context("engine", "alpha 1.5.3")


@mew.benchmark(name="scan")
def bench_scan(state):
    for _ in state:
        ...
```

Use the same `name=` on both sides so the rows line up. `compare` matches on the
function name when pivoting, so the file prefix differing is fine.

## Run them interleaved

```console
$ for i in 1 2 3 4 5; do
>   mew run bench_alpha.py --session-tag ab --append -o results.jsonl
>   mew run bench_beta.py  --session-tag ab --append -o results.jsonl
> done
$ mew compare results.jsonl --by custom.engine
```

Two things make this work:

- **Interleaving** (A B A B …, not AAAAA BBBBB) decorrelates thermal and load
  drift from the axis you are comparing, so the second suite is not
  systematically penalised for running later.
- **The shared `--session-tag`** makes all ten runs one session, so every
  repetition feeds the statistic. Without it each run is its own session and
  `compare` keeps only the newest. See {doc}`regressions` for the details.

If you are on one revision, the tag defaults to the jj change id / `git
describe` and you can drop `--session-tag` entirely.

```console
                       Comparison (real_time)
Benchmark │ alpha (baseline) │    beta │                Δ% │ speedup
────────────────────────────────────────────────────────────────────
scan      │          1.79 us │ 3.28 us │ +83.41% (signif.) │  ×0.545
```

`--baseline` picks which column is the reference (default: the first written).

## Different interpreters

Because it is just a loop, each side can run under whatever Python it needs:

```console
$ for i in 1 2 3; do
>   .venv-gil/bin/mew run bench_x.py --session-tag ft --append -o results.jsonl
>   .venv-ft/bin/mew  run bench_x.py --session-tag ft --append -o results.jsonl
> done
```

Here both sides are the *same* benchmark file, distinguished by the interpreter.
Record which is which from inside the suite, so the pivot has something to group
on:

```python
import sys

import mew

mew.set_context("build", "free-threaded" if not sys._is_gil_enabled() else "gil")
```

```console
$ mew compare results.jsonl --by custom.build
```

## Profiling both sides

The profiling flags work per invocation, so give each side its own artifact path:

```console
$ mew run bench_alpha.py --session-tag ab --append -o results.jsonl \
      --profile-memory --flamegraph alloc.alpha.html
$ mew run bench_beta.py  --session-tag ab --append -o results.jsonl \
      --profile-memory --flamegraph alloc.beta.html
$ mew compare results.jsonl --by custom.engine --metric memory.allocations_per_iteration
```

Use `memory.allocations_per_iteration` for cross-engine allocation comparisons:
a faster engine runs more iterations, inflating the cumulative
`total_allocations` for the same per-call work (see {doc}`profiling-memory`).

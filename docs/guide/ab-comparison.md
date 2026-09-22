# A/B comparison across processes

Some comparisons cannot share an interpreter: two engines whose extension
modules statically link different versions of the same native library, a GIL
build against a free-threaded one, a release build against an AddressSanitizer
one. Run each side in its own process and merge the results into one file.

mew has no orchestrator for this; a shell loop does it, and can use a different
interpreter per side.

## Label each suite

Record what each side is in its context, so the comparison documents the
version skew, and use the same `name=` on both sides so the rows line up:

```python
# bench_alpha.py
import mew

mew.update_context(mew.vcs_context())  # records the commit both sides ran at
mew.set_context("engine", "alpha 1.5.3")


@mew.benchmark(name="scan")
def bench_scan(state):
    for _ in state:
        ...
```

## Run them interleaved

Write each side to its own file. `--append` accumulates the repetitions:

```console
$ for i in 1 2 3 4 5; do
>   mew run bench_alpha.py --append -o alpha.jsonl
>   mew run bench_beta.py  --append -o beta.jsonl
> done
$ mew compare --key func beta.jsonl alpha.jsonl
```

Interleave the suites (A B A B …, not AAAAA BBBBB) to avoid bias from thermal
and load drift. `--key func` matches on the function name, so the differing
file prefixes do not matter. Differing context values (`engine=…`) are shown in
the column labels.

```console
                       Comparison (real_time)
Benchmark │ alpha (baseline) │    beta │                Δ% │ speedup
────────────────────────────────────────────────────────────────────
scan      │          1.79 us │ 3.28 us │ +83.41% (signif.) │  ×0.545
```

To keep both sides in one archive instead, give each run a `--session-tag` and
address them as `results.jsonl@beta results.jsonl@alpha`; see
[](regressions.md#comparing-sessions-in-one-file).

## Different interpreters

Each side can run under whatever Python it needs:

```console
$ for i in 1 2 3; do
>   .venv-gil/bin/mew run bench_x.py --append -o gil.jsonl
>   .venv-ft/bin/mew  run bench_x.py --append -o ft.jsonl
> done
$ mew compare ft.jsonl gil.jsonl
```

Here both sides are the *same* benchmark file, distinguished by the interpreter.
Record which is which from inside the suite, so the column labels say so:

```python
import sys

import mew

mew.update_context(mew.vcs_context())
mew.set_context("build", "free-threaded" if not sys._is_gil_enabled() else "gil")
```

## Profiling both sides

The profiling flags work per invocation, so give each side its own artifact path:

```console
$ mew run bench_alpha.py -o alpha.jsonl --profile-memory --flamegraph alloc.alpha.html
$ mew run bench_beta.py  -o beta.jsonl  --profile-memory --flamegraph alloc.beta.html
$ mew compare --key func --metric memory.allocations_per_iteration beta.jsonl alpha.jsonl
```

Use `memory.allocations_per_iteration` for cross-engine allocation comparisons:
a faster engine runs more iterations, inflating the cumulative
`total_allocations` for the same per-call work (see {doc}`profiling-memory`).

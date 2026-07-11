# Variants: side-by-side runs

`mew run --variant name=path` runs the *same logical suite* as several
independent processes and merges the results into one file, tagged by variant.
It's the tool for comparisons that **can't share an interpreter**:

- rival engines that statically link the same native library (e.g. two DuckDB
  bindings),
- GIL vs free-threaded interpreters, or two Python versions,
- a Release build vs an AddressSanitizer build.

Each variant is a `name=path` pair, repeatable. `--variant` is mutually
exclusive with positional paths.

```console
$ mew run --variant duckdb=benchmarks/bench_duckdb.py \
          --variant ducky=benchmarks/bench_ducky.py \
          -o results.jsonl
```

## How it runs

Each variant runs in its **own subprocess**, so incompatible libraries never
share an address space. Repetitions run in **repetition-major** order (rep 0: A
B, rep 1: A B, …), which decorrelates thermal and load drift from the variant
axis — the second variant isn't systematically penalised for running later.
Every row lands in one file, sharing a `session_id` and carrying its `variant`
name and repetition index.

Reporters work unchanged: the live table gains a `Variant` column, and
`-o results.{jsonl,jsonl.gz,json}` captures the merged file.

```console
$ mew run --variant duckdb=bench_duckdb.py --variant ducky=bench_ducky.py --repetitions 5
mew · host=laptop cpus=10 …
Benchmark                  │ Variant │    Iters │    Real │     CPU
────────────────────────────────────────────────────────────────────
bench_scan.py::bench_scan  │ duckdb  │  200,000 │  4.8 µs │  4.7 µs
bench_scan.py::bench_scan  │ ducky   │  240,000 │  4.1 µs │  4.0 µs
…
```

`--min-time`, `--min-warmup-time`, and `--random-interleaving` apply to each
variant; `--repetitions N` runs each variant N times, interleaved as above. If a
variant fails, mew warns on stderr, keeps the rows that did land, and exits
nonzero.

## Comparing variants

A `--variant` run produces a single file with several variant columns, so
compare it with `--by variant` (not two file arguments):

```console
$ mew compare results.jsonl --by variant
$ mew compare results.jsonl --by variant --baseline duckdb   # pick the baseline column
```

`--by variant` defaults `--key` to `func`; every variant shares the same
`file.py::` prefix, so columns line up on the function name automatically. The
first variant written is the baseline unless `--baseline` says otherwise. See
[](regressions.md) for the matching rules and metrics.

## Per-variant context

Call {func}`mew.set_context` in each benchmark file to record what makes that
variant different, usually the engine and its version. Each variant's context
is kept separately and surfaces as a per-column annotation, so version skew
documents itself:

```python
# bench_duckdb.py
import mew

mew.set_context("engine", "duckdb 1.5.3")
```

```console
$ mew compare results.jsonl --by variant
duckdb (engine=duckdb 1.5.3): session=v0.4.1 host=laptop cpus=10 …
ducky (engine=ducky 0.2.0):   session=v0.4.1 host=laptop cpus=10 …
                       Comparison (real_time)
┏━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━┳━━━━━━━━┳━━━━━━━━━┓
┃ Benchmark   ┃ duckdb (baseline)  ┃ ducky             ┃     Δ% ┃ speedup ┃
┡━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━╇━━━━━━━━╇━━━━━━━━━┩
│ bench_scan  │           4.80 µs  │          4.10 µs  │ -14.6% │  ×1.171 │
└─────────────┴────────────────────┴───────────────────┴────────┴─────────┘
```

## Profiling across variants

All [profiling](profiling-memory.md) flags compose with `--variant`: each variant
gets its own profile pass, so cross-engine **memory** and **CPU** comparisons
come out of a single run.

```console
$ mew run --variant duckdb=bench_duckdb.py --variant ducky=bench_ducky.py \
          --profile-memory -o results.jsonl
$ mew compare results.jsonl --by variant --metric memory.allocations_per_iteration
```

Use `memory.allocations_per_iteration` for cross-engine allocation comparisons:
a faster engine runs more iterations, inflating the cumulative
`total_allocations` for the same per-call work (see [](profiling-memory.md)).
HTML artifacts (`--flamegraph`, `--sample-html`) are
written one per variant, with the variant name spliced into the filename
(`alloc.html` → `alloc.duckdb.html`), so the variants don't overwrite each other.

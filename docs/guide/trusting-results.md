# Trusting your numbers

Machine state can outweigh the change under test. mew records provenance, warns
about mismatched environments, and marks noisy results.

## Read the context header

Every run prints one provenance line before the table:

```console
$ mew run
mew · cpu=Apple M1 Pro cpus=10 scaling=enabled
```

`scaling=enabled` means CPU frequency may change during measurement, making
absolute timings unstable.

On Linux, pin the governor to `performance` with `cpupower` or an equivalent.
macOS does not provide a supported way to disable scaling; prefer relative A/B
measurements there.

:::{tip}
With scaling enabled, prefer interleaved A/B measurements over historical
absolute values.
:::

## Give each benchmark enough time

Google Benchmark increases the iteration count until the body reaches `min_time`.

```console
$ mew run --min-time 0.5          # seconds; or 500ms, 1m
$ mew run --min-time 100x         # or an exact iteration count
$ mew run --min-warmup-time 200ms # warm caches before measuring
```

Raising `min_time` reduces sampling error, but not clock or system-load drift.

## Quantify the noise before you trust a delta

`--repetitions` reruns the benchmark, including warm-up, to measure its spread:

```console
$ mew run --repetitions 10 -o head.json
$ mew compare baseline.json head.json
```

`compare` reduces each benchmark's per-repetition rows to their median, and
computes the coefficient of variation (stddev / mean).
Each value carries it as `±N%`; above **25%** it is flagged red as `±N% (!)`:

```text
Benchmark   │      baseline │             head │      Δ% │ speedup
──────────────────────────────────────────────────────────────────
bench_parse │ 1.20 us ±3.1% │ 1.44 us ±26% (!) │ +20.00% │  ×0.833
```

Treat a flagged row as inconclusive. Improve the environment or collect more
repetitions before interpreting its delta.

## Tell a real delta from noise

With at least two repetitions on each side, `compare` runs a Mann-Whitney U test
and marks deltas with p < 0.05 as `(signif.)`. With up to 20 repetitions on each
side and no tied values, the p-value is exact; otherwise it uses the normal
approximation with tie and continuity corrections. Few repetitions cap the
evidence: with 3 on each side the smallest possible p-value is 0.1, so no delta
is marked; 4 on each side is the fewest that can be.

```text
Benchmark    │ baseline │    head │                Δ% │ speedup
────────────────────────────────────────────────────────────────
bench_parse  │  1.20 us │ 1.44 us │ +20.00% (signif.) │  ×0.833
```

A marker is evidence against equal distributions. Its absence is not evidence
of equality, especially with few repetitions.

## Decorrelate what you can't control

Interleaving reduces ordering bias from thermal and background-load drift:

```console
$ mew run --repetitions 10 --random-interleaving
```

This requires `--repetitions > 1`.

For comparisons across processes, see {doc}`ab-comparison`.

## Don't compare across machines

`compare` prints one provenance line per column and warns on stderr when files
disagree on the CPU model, CPU count, or CPU scaling, or on a `host` that the
suite records (see [](context.md#recording-the-host)):

```console
warning: result files differ in cpu_model (baseline: AMD EPYC 7763, head: Apple M1 Pro);
deltas may reflect the environment, not the code
```

Treat this warning as invalidating. Context values that differ
between the files, custom {doc}`context <context>` included, are shown on each
file's context line above the table.

## A defensible CI gate

Measure long enough, collect repetitions, interleave them, and use a baseline
from the same runner:

```console
$ mew run --min-time 0.5 --repetitions 10 --random-interleaving -o head.json
$ mew compare baseline.json head.json \
      --regression-threshold 5% --exit-non-zero-on-regression
```

The gate exits with code 3 when a benchmark regresses past the threshold, and
with 1 or 2 when there is nothing to compare or the command is wrong (see
[](cli.md#exit-codes)). See {doc}`regressions` for per-benchmark thresholds and
the allowlist that keeps known-noisy benchmarks from failing the build.

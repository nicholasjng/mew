# Trusting your numbers

Machine state can outweigh the change under test. mew records provenance, warns
about mismatched environments, and marks noisy results.

## Read the context header

Every run prints one provenance line before the table:

```console
$ mew run
mew · host=laptop cpus=10 scaling=enabled
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
$ mew run --min-time 0.5          # seconds
$ mew run --min-time 100x         # or an exact iteration count
$ mew run --min-warmup-time 200ms # warm caches before measuring
```

Raising `min_time` reduces sampling error, but not clock or system-load drift.

## Quantify the noise before you trust a delta

`--repetitions` reruns the benchmark, including warm-up, to measure its spread:

```console
$ mew run --repetitions 10 -o head.json
$ mew compare head.json baseline.json --stddev
```

`compare` reduces each benchmark's per-repetition rows itself — median by
default, `--statistic` for `mean`, `p95`, `gmean`, or your own reducer — and
computes the coefficient of variation (stddev / median). Any row above **25%** is
flagged red as `±N% (!)`:

```text
Benchmark    │ baseline │ ± stddev │      head │      Δ% │ speedup
────────────────────────────────────────────────────────────────────
bench_parse  │ 1.20 µs  │  0.31 µs │   1.44 µs │ +20.0%  │ ×0.83 ±26% (!)
```

Treat a flagged row as inconclusive. Improve the environment or collect more
repetitions before interpreting its delta.

:::{warning}
Do not use `report_aggregates_only=True` for files passed to `mew compare`.
Comparison statistics require per-repetition rows; Google Benchmark's aggregate
rows are discarded.
:::

## Tell a real delta from noise

With at least two repetitions on each side, `compare` runs a Mann-Whitney U test
and marks deltas with p < 0.05 as `(signif.)`:

```text
Benchmark    │ baseline │      head │              Δ% │ speedup
─────────────────────────────────────────────────────────────────
bench_parse  │ 1.20 µs  │   1.44 µs │ +20.0% (signif.) │ ×0.83
```

A marker is evidence against equal distributions. Its absence is not evidence
of equality, especially with few repetitions.

## Decorrelate what you can't control

Interleaving reduces ordering bias from thermal and background-load drift:

```console
$ mew run --repetitions 10 --random-interleaving
```

This requires `--repetitions > 1`.

For an A/B between two implementations, {doc}`ab-comparison` applies the same
idea across processes: alternate the two suites (rep 0: A B, rep 1: A B, …)
rather than running one to completion first, so drift hits both sides equally
instead of accumulating against the second one.

## Don't compare across machines

`compare` prints one provenance line per column and warns on stderr when files
disagree on `host_name`, `num_cpus`, or CPU scaling:

```console
warning: result files differ in host_name (baseline: ci-runner-3, head: laptop);
deltas may reflect the environment, not the code
```

Treat this warning as invalidating. Differing custom {doc}`context <context>`
values are shown in column labels.

## A defensible CI gate

Measure long enough, collect repetitions, interleave them, and use a baseline
from the same runner:

```console
$ mew run --min-time 0.5 --repetitions 10 --random-interleaving -o head.json
$ mew compare head.json baseline.json \
      --regression-threshold 5% --exit-non-zero-on-regression
```

The gate exits 2 when a benchmark regresses past the threshold. See
{doc}`regressions` for per-benchmark thresholds and the allowlist that keeps
known-noisy benchmarks from failing the build.

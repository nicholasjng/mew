# Trusting your numbers

A microbenchmark reports what the machine did, not what your code costs. On a
laptop or a shared CI runner, the gap between the two is routinely larger than
the change you're trying to measure. mew surfaces that gap rather than averaging
it away: runs print their provenance, `compare` warns before diffing across
machines, and noisy rows are marked. Here is how to read those signals.

## Read the context header

Every run prints one provenance line before the table:

```console
$ mew run
mew · host=laptop cpus=10 @ 3200MHz scaling=enabled
```

`scaling` is the one to look at. It comes from Google Benchmark's own probe:
`enabled` means the CPU frequency governor is free to move the clock while your
benchmark runs, so two runs of identical code can differ by more than a real
regression would. Absolute nanosecond figures are then not comparable *even
against yourself*.

On Linux you can fix the clock for the duration of a run by pinning the governor
to `performance` (via `cpupower`, or your distribution's equivalent). On macOS
there is no supported way to disable it, so treat every absolute number from a
Mac as indicative and lean on the relative techniques below.

:::{tip}
`scaling=enabled` is a reason to compare *within* one run, not to skip
benchmarking. An A/B against a baseline measured minutes ago on the same clock
beats an absolute figure compared against last week's.
:::

## Give each benchmark enough time

Google Benchmark picks the iteration count itself, doubling until the body has
run for `min_time`. The default is tuned for fast feedback, not for stability.

```console
$ mew run --min-time 0.5          # seconds
$ mew run --min-time 100x         # or an exact iteration count
$ mew run --min-warmup-time 200ms # warm caches before measuring
```

Raising `min_time` shrinks *sampling* error — the noise from timing too few
iterations. It does nothing about drift: if the clock or the machine's load moves
over the run, a longer measurement just averages over more of the drift. That is
what the next two sections are for.

## Quantify the noise before you trust a delta

A single number has no error bar. `--repetitions` re-runs the whole benchmark,
warm-up included, so there's a spread to look at:

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

Read that marker as *this row cannot support a conclusion*, whatever the Δ%
says. A 20% "regression" inside a 26% spread is noise. Fix the environment or
raise the repetition count until the flag clears, then re-read the delta.

:::{warning}
`report_aggregates_only=True` (or `--repetitions` with aggregates-only
configured) removes a benchmark from `mew compare` **silently**. `compare`
discards Google Benchmark's aggregate rows and recomputes statistics from the
per-repetition rows, so a file with only `_mean`/`_median`/`_stddev` rows has
nothing left to compare: the benchmark vanishes from the table with no warning
and exit code 0. Use aggregates-only for terminal output you read by eye, never
for files you intend to diff.
:::

## Tell a real delta from noise

The CV marker flags a row you *can't* trust; it says nothing about the rows
that pass. With the same `--repetitions` data, `compare` also runs a
Mann-Whitney U test between the two sides' per-repetition values and marks
deltas that clear the conventional p < 0.05 bar as `(signif.)`:

```text
Benchmark    │ baseline │      head │              Δ% │ speedup
─────────────────────────────────────────────────────────────────
bench_parse  │ 1.20 µs  │   1.44 µs │ +20.0% (signif.) │ ×0.83
```

Only the deltas worth a second look get marked. Most rows in a healthy
comparison are noise around zero; flagging every one would bury the one that
matters. An unmarked delta isn't proven noise (the test is underpowered at
low repetition counts), but a marked one probably isn't. Needs 2+ repetitions
on both sides; below that, no marker either way, same as the CV marker.

## Decorrelate what you can't control

Thermal throttling and background load drift over minutes, so whichever
benchmark runs last is systematically disadvantaged. Interleaving turns that
systematic bias into noise you can see:

```console
$ mew run --repetitions 10 --random-interleaving
```

This shuffles repetitions across benchmarks instead of running each benchmark's
repetitions back to back (it needs `--repetitions > 1` to do anything).

For an A/B between two implementations, {doc}`variants` goes further: each
variant runs in its own subprocess and the parent drives them in
repetition-major order (rep 0: A B, rep 1: A B, …), so drift hits both sides
equally instead of accumulating against the second one.

## Don't compare across machines

`compare` prints one provenance line per column and warns on stderr when files
disagree on `host_name`, `num_cpus`, or CPU scaling:

```console
warning: result files differ in host_name (baseline: ci-runner-3, head: laptop);
deltas may reflect the environment, not the code
```

Treat that warning as invalidating, not advisory. Custom
{doc}`context <context>` keys that differ across files are appended to the column
labels instead, so a deliberate apples-to-oranges run documents itself in its own
output.

## A defensible CI gate

Putting it together — measure long enough, quantify the spread, interleave, and
compare only against a baseline from the same runner:

```console
$ mew run --min-time 0.5 --repetitions 10 --random-interleaving -o head.json
$ mew compare head.json baseline.json \
      --regression-threshold 5% --exit-non-zero-on-regression
```

The gate exits 2 when a benchmark regresses past the threshold. See
{doc}`regressions` for per-benchmark thresholds and the allowlist that keeps
known-noisy benchmarks from failing the build.

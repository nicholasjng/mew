# Comparisons and regression gating

`mew compare` diffs two or more result files (`.json`, `.jsonl`, or `.jsonl.gz`; every sink `mew run -o` writes).
The last is the baseline; earlier files are diffed against it (`mew compare head.json baseline.json` reads like "compare head against baseline").
With `--regression-threshold` it also computes and prints a regression panel; add `--exit-non-zero-on-regression` to turn that into a CI gate, returning exit code 2 when any benchmark drifts in the wrong direction by more than the threshold.

## Basic comparison

```console
$ mew compare head.json baseline.json
$ mew compare --metric cpu_time head.json baseline.json
$ mew compare --pattern 'sort' head.json baseline.json
$ mew compare --stddev head.json baseline.json    # show stddev cols if present
```

Supported metrics: `real_time` (default), `cpu_time`, `iterations`.
For `iterations`, higher is better, so the regression direction is inverted under the hood.
Files produced with `--profile-memory` additionally support `memory.peak_bytes` and `memory.allocations_per_iteration`:

```console
$ mew compare -m memory.peak_bytes head.json baseline.json
$ mew compare -m memory.allocations_per_iteration ducky.jsonl duckdb.jsonl
```

Prefer `memory.allocations_per_iteration` for cross-engine / cross-run comparisons: `memory.total_allocations` is cumulative over the (run-dependent) iteration count, so a faster engine reports a higher raw total for the same per-call work. `peak_bytes` is a high-water mark and comparable as-is.

## Matching benchmarks across suites

By default, benchmarks are matched by their full registered name (`file.py::func`), which is right for before/after comparisons of the same suite.
For A/B suites in different files (two engine bindings, two implementations), the file prefix makes every name unique, so nothing overlaps.
`--key func` matches on the function name alone:

```console
$ mew run benchmarks/bench_ducky.py -o ducky.jsonl
$ mew run benchmarks/bench_duckdb.py -o duckdb.jsonl
$ mew compare --key func ducky.jsonl duckdb.jsonl
```

If stripping the prefix makes two benchmarks in one file collide, `compare` exits with an error rather than guessing.

When comparing variants within a single `mew run --variant` result file (`mew compare --by variant results.jsonl`), `--key` defaults to `func` automatically; every variant carries the same file prefix, so matching on the function name is what lines the columns up.

Parametrize cases are rendered and matched by their human id (`bench_udf_scalar[n=10000]` rather than Google Benchmark's raw `bench_udf_scalar/case:0`), and per-benchmark option suffixes like `/min_time:0.200` are ignored for matching, so files run with different options still align.

## Context and noise

Each file's context block is printed above the table (host, CPU count, scaling, date, plus any `mew.set_context()` values), and `compare` warns on stderr when files differ in `host_name`, `num_cpus`, or CPU scaling; deltas then reflect the environment, not the code.
Custom context keys whose values differ across files (e.g. `engine=duckdb 1.5.3`) are appended to the column labels, so an apples-vs-oranges comparison documents itself.

When a file contains per-repetition rows (`--repetitions N`), rows whose coefficient of variation exceeds 25% are flagged with a red `±N% (!)` marker: their median is too noisy to trust, regardless of what the delta says.

(comparing-sessions-in-one-file)=
## Comparing sessions in one file

Each `mew run` is one *session* (see [](context.md#session-identity)). Normally each run writes its own file and you compare files, the recommended shape for CI. For local before/after experiments, though, it's handy to keep both runs in one file with `--append`, then address them by `path@selector`:

```console
$ mew run --session-tag before -o results.jsonl
# ... change something ...
$ mew run --session-tag after --append -o results.jsonl
$ mew compare results.jsonl@after results.jsonl@before
```

A selector picks one session from a multi-session file:

- `@latest` / `@earliest`: by recency.
- `@~N`: N sessions back from the latest (`@~0` is latest, `@~1` the one before).
- `@<tag>`: exact `session_tag` match.
- `@<id-prefix>`: a `session_id` prefix, at least 4 characters.

Ambiguous tag/prefix matches and misses are errors, so a selector always resolves to exactly one session. Without a selector, `compare` uses the latest session per benchmark and warns about discarded older ones (so a plain two-file comparison is unchanged). A file whose name genuinely contains `@` is taken literally as long as it exists on disk.

This is deliberately not a query engine over a growing archive: for "the most recent master run in a rolling history file", do the selection upstream in SQL (DuckDB/polars) and hand `compare` a file with just the two sessions you want.

## Gating CI

```console
$ mew compare --regression-threshold 5% --exit-non-zero-on-regression head.json baseline.json
```

The threshold is a percent, and the `%` is required (`--regression-threshold 5` is a CLI error, not a silent "5 percent"). `--regression-threshold` alone always prints the regression panel; only `--exit-non-zero-on-regression` turns a regression into a nonzero exit — so a CI workflow only fails once you opt in, and you can calibrate thresholds locally by watching the panel without risking a red build.

## Allowlist

Keep an allowlist of expected drift in `pyproject.toml`:

```toml
[tool.mew.regressions]
default_threshold = 5.0

[[tool.mew.regressions.allow]]
pattern = "benchmarks/bench_io.py::*"
threshold = 15.0
reason = "I/O is noisy on the CI runner; raise the bar."

[[tool.mew.regressions.allow]]
pattern = "*[algo='bubble']"
ignore = true
reason = "Bubble sort is intentionally slow; skip the gate."
```

Patterns use {func}`fnmatch.fnmatchcase` against the full benchmark name.
Each rule must include a `reason` so the allowlist stays explainable. A
rule must either set `ignore=true` or `threshold=<float>`.

## Verdicts

| Verdict         | Meaning                                                                          |
| --------------- | -------------------------------------------------------------------------------- |
| `OK`            | Within the active threshold.                                                     |
| `REGRESSED`     | Over the default threshold, no rule matched; fails the gate.                    |
| `ALLOWED_OVER`  | Over the default, but a rule raised the bar. Shown as a warning, not a failure. |
| `IGNORED`       | A matching rule says skip gating entirely. Listed for visibility.                |

The panel printed to stderr surfaces all four buckets; the regressed list drives the exit code (only when `--exit-non-zero-on-regression` is set).

## A typical CI workflow

```yaml
- name: Restore baseline
  uses: actions/cache@v4
  with: { path: baseline.json, key: bench-baseline-${{ github.base_ref }} }

- name: Run benchmarks
  run: mew run --min-time 1s -o head.json

- name: Gate on regressions
  run: mew compare --regression-threshold 5% --exit-non-zero-on-regression head.json baseline.json
```

Then persist `head.json` (e.g. via `actions/cache` keyed on the merged SHA) so the next run on `main` becomes the next baseline.

# Comparisons and regression gating

`mew compare` diffs `.json`, `.jsonl`, or `.jsonl.gz` results. The last file is the baseline.
Add a regression threshold and exit flag to use it as a CI gate.
With more than two files, the gate compares the first file against the last;
intermediate files are informational. Missing historical measurements appear as
dashes and do not exclude a benchmark from gating.

## Basic comparison

```console
$ mew compare head.json baseline.json
$ mew compare --metric cpu_time head.json baseline.json
$ mew compare --pattern 'sort' head.json baseline.json
$ mew compare --stddev head.json baseline.json    # show stddev cols if present
```

Supported metrics: `real_time` (default), `cpu_time`, `iterations`.
For `iterations`, higher is better, so the regression direction is inverted.
Files produced with `--profile-memory` additionally support `memory.peak_bytes` and `memory.allocations_per_iteration`:

```console
$ mew compare -m memory.peak_bytes head.json baseline.json
$ mew compare -m memory.allocations_per_iteration ducky.jsonl duckdb.jsonl
```

Use `memory.allocations_per_iteration` across runs because total allocations
depend on the memory-pass iteration count. `peak_bytes` is comparable as-is.

## Matching benchmarks across suites

The default key is the full registered name (`file.py::func`).
For equivalent benchmarks in different files, use `--key func`:

```console
$ mew run benchmarks/bench_ducky.py -o ducky.jsonl
$ mew run benchmarks/bench_duckdb.py -o duckdb.jsonl
$ mew compare --key func ducky.jsonl duckdb.jsonl
```

If stripping the prefix makes two benchmarks in one file collide, `compare` exits with an error rather than guessing.

Rows match on their `benchmark` field: parametrized cases by their labels
(`bench_sort[n=10]`), thread counts as a dimension (`/threads:2`). Per-benchmark
options such as `min_time` do not affect matching. Time measurements are
converted to a common unit before reducing repetitions.

## Context and noise

Context is printed above each column. `compare` warns when machine properties
differ and adds differing custom values to column labels.

For repeated measurements, a red `±N% (!)` marks a coefficient of variation
above 25%. Treat that delta as unreliable.

(comparing-sessions-in-one-file)=
## Comparing sessions in one file

Each `mew run` is one *session* (see [](context.md#session-identity)). A file
that holds several, because it was written with `--append`, contributes its
newest session by default; `mew sessions FILE` lists them. To pick a different
one, tag the runs and address them with `path@<tag>`:

```console
$ mew run --session-tag before -o results.jsonl
# ... change something ...
$ mew run --session-tag after --append -o results.jsonl
$ mew compare results.jsonl@after results.jsonl@before
```

`path@latest` names the newest session explicitly. Every session carrying a
tag is selected, so repeated runs under one tag pool as repetitions:

```console
$ for i in 1 2 3 4 5; do
>   mew run bench_a.py --session-tag a --append -o results.jsonl
>   mew run bench_b.py --session-tag b --append -o results.jsonl
> done
$ mew compare --key func results.jsonl@b results.jsonl@a
```

Interleaving decorrelates thermal and load drift from the axis you are
comparing, and all five repetitions of each side feed the statistic. Two
files, one per side, work the same way without tags (see {doc}`ab-comparison`).
Existing filenames that contain `@` are treated literally.

For anything richer than "this session against that one", query the archive
directly; see [](reporters.md#sql-and-dataframe-recipes).

## Gating CI

```console
$ mew compare --regression-threshold 5% --exit-non-zero-on-regression head.json baseline.json
```

The `%` suffix is required. A threshold prints the panel;
`--exit-non-zero-on-regression` additionally returns exit code 2 on regression.

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
pattern = "*[algo=bubble]"
ignore = true
reason = "Bubble sort is intentionally slow; skip the gate."
```

Rules are read from the `pyproject.toml` that `[tool.mew]` configuration comes
from. Patterns use {func}`fnmatch.fnmatchcase` against the full benchmark name.
Each rule must include a `reason` so the allowlist stays explainable. A
rule must either set `ignore=true` or `threshold=<float>`.

## Verdicts

| Verdict         | Meaning                                                                          |
| --------------- | -------------------------------------------------------------------------------- |
| `OK`            | Within the active threshold.                                                     |
| `REGRESSED`     | Over the default threshold, no rule matched; fails the gate.                    |
| `ALLOWED_OVER`  | Over the default, but a rule raised the bar. Shown as a warning, not a failure. |
| `IGNORED`       | A matching rule says skip gating entirely. Listed for visibility.                |

The panel goes to stderr. `REGRESSED` controls the optional nonzero exit.

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

Persist `head.json` after merging so it becomes the next baseline.

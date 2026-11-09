# Comparisons and regression gating

`mew compare` diffs `.json`, `.jsonl`, or `.jsonl.gz` results. The first file is the baseline.
Add a regression threshold and exit flag to use it as a CI gate.
With more than two files, the second is the candidate and the files after it are
informational. The table lists the benchmarks that the baseline and the candidate
share, with or without a gate; a later file that lacks one shows dashes.
The gate compares the candidate against the baseline, and a line under the table
sums it up: how many benchmarks got significantly slower or faster, and how many
did not change significantly.

## Basic comparison

```console
$ mew compare baseline.json head.json
$ mew compare --metric cpu_time baseline.json head.json
$ mew compare --pattern 'sort' baseline.json head.json
```

Supported metrics: `real_time` (default) and `cpu_time`.
Files produced with `--memory-profiler memray` additionally support `memory.peak_bytes` and `memory.allocations_per_iteration`:

```console
$ mew compare -m memory.peak_bytes baseline.json head.json
$ mew compare -m memory.allocations_per_iteration duckdb.jsonl ducky.jsonl
```

Use `memory.allocations_per_iteration` across runs because total allocations
depend on the memory-pass iteration count. `peak_bytes` is comparable as-is.

`counters.<name>` compares a counter that benchmarks set with
`state.set_counter()`, `set_items_processed()` or `set_bytes_processed()`:

```console
$ mew compare -m counters.items_per_second baseline.json head.json
$ mew compare -m counters.tlb_misses --regression-threshold 5% baseline.json head.json
```

Which direction is an improvement comes from `[[tool.mew.counters]]` rules in
`pyproject.toml`. Each rule has a `pattern`, which matches counter names as
allowlist patterns match benchmark names, with `*` and `?` as wildcards, and a
`higher-is-better`. The first matching rule applies:

```toml
[[tool.mew.counters]]
pattern = "l1_misses"
higher-is-better = true  # ahead of the family rule below

[[tool.mew.counters]]
pattern = "*_misses"
higher-is-better = false
```

`items_per_second` and `bytes_per_second`, from `set_items_processed()` and
`set_bytes_processed()`, are higher-is-better unless a rule says otherwise. A
counter no rule matches, such as an input size, has no direction: it compares
as a plain ratio, its delta neither good nor bad, and gating on it fails: a gate that skipped it would pass without checking
anything. As in every mew table, an unknown key in a rule is an error.
Benchmarks without the counter are left out.

A counter can be negative. Its delta is then measured against the baseline's
size, so -10 to -5 is +50%, and its ratio shows `—`: a ratio jumps sign where
the values cross zero, and a quantity that goes negative has no zero to be a
multiple of.

## Matching benchmarks

Rows match on their full `name`, `file.py::func`, relative to the project
root: parametrized cases by their labels (`bench_sort[n=10]`), thread counts
as a dimension (`/threads:2`). Per-benchmark options such as `min_time` do not
affect matching. To compare two implementations, run one benchmark file
against each; see {doc}`ab-comparison`.

## Context and noise

Each file gets a context line above the table: its run's name (or ID) and
date, then only the context values that differ between the files. `compare`
warns when machine properties differ. When all rows share one file prefix, such
as `benchmarks/bench_sort.py::`, the title names it once and the rows drop it.

A benchmark that a file has as skipped is reported with its reason
(`mew: warning: skipped in head.json: …`), apart from benchmarks the file lacks.

For repeated measurements, each value carries its spread: the coefficient of
variation (stddev over the mean of the repetitions),
as `±N%`. Above 25% it turns red as `±N% (!)`; treat that delta as unreliable.

(files-with-several-runs)=
## Files with several runs

Each `mew run` is one *run* (see [](context.md#run-identity)), and `mew log
FILE` lists the runs a file holds. A file is one sample: the runs `--append`
added to it pool as repetitions. Runs from different machines, interpreters,
mew versions or commits would mix environments, so `mew compare` warns when the
pooled runs differ in them, and each file's context line says how many runs it
pooled.

To compare runs against each other, write each to its own file. To pull one
run out of a growing archive, extract it first, e.g. by its `--run-name`; for
that and anything richer than "this file against that one", query the archive
directly: see [](reporters.md#sql-and-dataframe-recipes).

## Gating CI

```console
$ mew compare --regression-threshold 5% --exit-non-zero-on-regression baseline.json head.json
```

The `%` suffix is required. A threshold prints the panel;
`--exit-non-zero-on-regression` additionally exits with code 3 on regression,
apart from the other [exit codes](cli.md#exit-codes).
`--regression-threshold` overrides `default-threshold` from `pyproject.toml`
(see below); without either, the threshold is 5%.

## Allowlist

Keep an allowlist of expected drift in `pyproject.toml`:

```toml
[tool.mew.regressions]
default-threshold = 5.0

[[tool.mew.regressions.allow]]
pattern = "benchmarks/bench_io.py::*"
threshold = 15.0
reason = "I/O is noisy on the CI runner; raise the bar."

[[tool.mew.regressions.allow]]
pattern = "*[algo=bubble]"
threshold = inf
reason = "Bubble sort is intentionally slow; skip the gate."
```

Rules are read from the `pyproject.toml` that `[tool.mew]` configuration comes
from. Patterns use `*` and `?` wildcards (brackets match literally) against the
benchmark name, `file.py::func[label]`.
Each rule must include a `reason` so the allowlist stays explainable, and a
`threshold` in percent that replaces the default for matching benchmarks:
looser, stricter, or `inf` to exempt them. An exempt benchmark is still listed
in the panel when it moves past the default threshold.
As in `[tool.mew]` (see {doc}`configuration`), keys are dashed, and an unknown
key, such as a misspelled `treshold` or `default_threshold`, is an error in the
table and in its rules.

## Verdicts

| Verdict         | Meaning                                                                          |
| --------------- | -------------------------------------------------------------------------------- |
| `OK`            | Within the active threshold.                                                     |
| `REGRESSED`     | Over the active threshold (the default, or a matching rule's); fails the gate.  |
| `ALLOWED_OVER`  | Over the default, but a rule raised the bar, `inf` included. Shown as a warning, not a failure. |

The panel goes to stderr. `REGRESSED` controls the optional exit code 3.

## A typical CI workflow

```yaml
- name: Restore baseline
  uses: actions/cache@v4
  with: { path: baseline.json, key: bench-baseline-${{ github.base_ref }} }

- name: Run benchmarks
  run: mew run --min-time 1s -o head.json

- name: Gate on regressions
  run: mew compare --regression-threshold 5% --exit-non-zero-on-regression baseline.json head.json
```

Persist `head.json` after merging so it becomes the next baseline.

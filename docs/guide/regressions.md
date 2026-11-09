# Comparisons and regression gating

`mew compare` diffs `.json`, `.jsonl`, or `.jsonl.gz` results. The last file is the baseline.
Add a regression threshold and exit flag to use it as a CI gate.
With more than two files, the first is the candidate and the files between are
informational. The table lists the benchmarks that the candidate and the baseline
share, with or without a gate; a file between them that lacks one shows dashes.
The gate compares the candidate against the baseline.

## Basic comparison

```console
$ mew compare head.json baseline.json
$ mew compare --metric cpu_time head.json baseline.json
$ mew compare --pattern 'sort' head.json baseline.json
$ mew compare --stddev head.json baseline.json    # show stddev cols if present
```

Supported metrics: `real_time` (default), `cpu_time`, `iterations`.
For `iterations`, higher is better, so the regression direction is inverted.
Files produced with `--memory-profiler memray` additionally support `memory.peak_bytes` and `memory.allocations_per_iteration`:

```console
$ mew compare -m memory.peak_bytes head.json baseline.json
$ mew compare -m memory.allocations_per_iteration ducky.jsonl duckdb.jsonl
```

Use `memory.allocations_per_iteration` across runs because total allocations
depend on the memory-pass iteration count. `peak_bytes` is comparable as-is.

`counters.<name>` compares a counter that benchmarks set with
`state.set_counter()`, `set_items_processed()` or `set_bytes_processed()`:

```console
$ mew compare -m counters.items_per_second head.json baseline.json
$ mew compare -m counters.tlb_misses --regression-threshold 5% head.json baseline.json
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

## Matching benchmarks across suites

The default key is the full registered name (`file.py::func`).
For equivalent benchmarks in different files, use `--key func`:

```console
$ mew run benchmarks/bench_ducky.py -o ducky.jsonl
$ mew run benchmarks/bench_duckdb.py -o duckdb.jsonl
$ mew compare --key func ducky.jsonl duckdb.jsonl
```

If stripping the prefix makes two benchmarks in one file collide, `compare` exits with an error rather than guessing.

Rows match on their `name`: parametrized cases by their labels
(`bench_sort[n=10]`), thread counts as a dimension (`/threads:2`). Per-benchmark
options such as `min_time` do not affect matching.

## Context and noise

Context is printed per file above the table. `compare` warns when machine
properties differ and adds differing custom values to that file's context line.

For repeated measurements, a red `±N% (!)` marks a coefficient of variation
(stddev over the mean of the repetitions, whatever the `--statistic`) above 25%.
Treat that delta as unreliable.

(comparing-runs-in-one-file)=
## Comparing runs in one file

Each `mew run` is one *run* (see [](context.md#run-identity)). A file that
holds several, because it was written with `--append`, contributes its newest
run by default; `mew log FILE` lists them. To pick a different one, name the
runs and address them with `path@<name>`:

```console
$ mew run --run-name before -o results.jsonl
# ... change something ...
$ mew run --run-name after --append -o results.jsonl
$ mew compare results.jsonl@after results.jsonl@before
```

`path@latest` names the newest run explicitly, so `latest` is reserved:
`--run-name latest` is an error. A name selects the newest run that has it, so
running again under a name replaces the earlier run in comparisons; runs are
never pooled, since two runs can come from different machines or interpreters.
For more repetitions, raise `--repetitions` in one run. Two files, one per
side, work the same way without names (see {doc}`ab-comparison`).
The selector follows the last `@` that comes after a `.json`, `.jsonl`, or
`.jsonl.gz` extension. Any other `@` is part of the file name, so
`runs@ci.jsonl` names a file.

For anything richer than "this run against that one", query the archive
directly; see [](reporters.md#sql-and-dataframe-recipes).

## Gating CI

```console
$ mew compare --regression-threshold 5% --exit-non-zero-on-regression head.json baseline.json
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
benchmark name as `mew compare` keys it: `file.py::func[label]` by default,
`func[label]` under `--key func`.
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
  run: mew compare --regression-threshold 5% --exit-non-zero-on-regression head.json baseline.json
```

Persist `head.json` after merging so it becomes the next baseline.

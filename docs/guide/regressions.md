# Comparisons and regression gating

`mew compare` diffs `.json`, `.jsonl`, or `.jsonl.gz` results. The last file is the baseline.
Add a regression threshold and exit flag to use it as a CI gate.

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

With `--by`, the key defaults to `func`.

Parametrized cases match by their human-readable IDs.
Google Benchmark option suffixes are ignored.

## Context and noise

Context is printed above each column. `compare` warns when machine properties
differ and adds differing custom values to column labels.

For repeated measurements, a red `±N% (!)` marks a coefficient of variation
above 25%. Treat that delta as unreliable.

(comparing-sessions-in-one-file)=
## Comparing sessions in one file

Each `mew run` is one *session* (see [](context.md#session-identity)). For local
experiments, append several sessions to one file and select them with `path@selector`:

```console
$ mew run --session-tag before -o results.jsonl
# ... change something ...
$ mew run --session-tag after --append -o results.jsonl
$ mew compare results.jsonl@after results.jsonl@before
```

### What counts as one session

Runs on one host that share a `session_tag` — or, absent one, the same
`context.vcs.commit` — are **one session**, so repeated runs at one revision
belong together: an interleaved A/B loop appending to one file reduces over
every repetition rather than keeping only the last run.

Record the commit from the suite:

```python
import mew

mew.update_context(mew.vcs_context())
```

{func}`mew.vcs_context` shells out to jj or git and returns `{"vcs": {...}}`
(backend, full commit, dirty flag, plus the jj change id or git branch), or `{}`
outside a work tree. It is opt-in: not every run wants to pay for a subprocess.
`--session-tag` overrides it, and is what you want when a comparison spans
revisions.

```console
$ for i in 1 2 3 4 5; do
>   mew run bench_a.py --append -o results.jsonl
>   mew run bench_b.py --append -o results.jsonl
> done
$ mew compare results.jsonl --by context.engine
```

Interleaving decorrelates thermal and load drift from the axis you are comparing,
and because both suites carry one tag, all five repetitions of each feed the
statistic. Runs with *different* tags (or none) stay separate, one per run.

A selector picks one session from a multi-session file:

- `@latest` / `@earliest`: by recency.
- `@~N`: N sessions back from the latest (`@~0` is latest, `@~1` the one before).
- `@<tag>`: exact `session_tag` match (one per host, since a tag groups its runs).
- `@<id-prefix>`: a `session_id` prefix, at least 4 characters.

Selectors must resolve uniquely. Without one, `compare` uses the latest session
per benchmark and warns when it discards older data. Existing filenames that
contain `@` are treated literally.

Selectors address sessions; use SQL or a dataframe tool for richer queries.

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

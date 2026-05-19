# Comparisons and regression gating

`mew compare` diffs two or more result files.
The first is the baseline; later files are diffed against it.
With `--fail-on-regression`, the command also acts as a CI gate that returns exit code 2 when any benchmark drifts in the wrong direction by more than the threshold.

## Basic comparison

```console
$ mew compare baseline.json head.json
$ mew compare --metric cpu_time baseline.json head.json
$ mew compare --pattern 'sort' baseline.json head.json
$ mew compare --stddev baseline.json head.json    # show stddev cols if present
```

Supported metrics: `real_time` (default), `cpu_time`, `iterations`.
For `iterations`, higher is better, so the regression direction is inverted under the hood.

## Gating CI

```console
$ mew compare --fail-on-regression 5 baseline.json head.json
```

The threshold is in percent.
With `--fail-on-regression 5`, any benchmark that's more than 5% slower than baseline causes a nonzero exit.
This way, a CI workflow can be constructed to fail directly from a failed comparison.

## Allowlist

Keep an allowlist of expected drift in `pyproject.toml`:

```toml
[tool.mew.regressions]
default_threshold_pct = 5.0

[[tool.mew.regressions.allow]]
pattern = "benchmarks/bench_io.py::*"
threshold_pct = 15.0
reason = "I/O is noisy on the CI runner — raise the bar."

[[tool.mew.regressions.allow]]
pattern = "*[algo='bubble']"
ignore = true
reason = "Bubble sort is intentionally slow; skip the gate."
```

Patterns use {func}`fnmatch.fnmatchcase` against the full benchmark name.
Each rule must include a `reason` so the allowlist stays explainable. A
rule must either set `ignore=true` or `threshold_pct=<float>`.

Inline allowlist entries are accepted for ad-hoc runs:

```console
$ mew compare --allow 'bench_io.py::*:15' --allow 'bench_bubble:*' \
              --fail-on-regression 5 baseline.json head.json
```

Bare patterns ignore; the `PATTERN:PCT` syntax raises the threshold to `PCT`.

## Verdicts

| Verdict         | Meaning                                                                          |
| --------------- | -------------------------------------------------------------------------------- |
| `OK`            | Within the active threshold.                                                     |
| `REGRESSED`     | Over the default threshold, no rule matched — fails the gate.                    |
| `ALLOWED_OVER`  | Over the default, but a rule raised the bar. Shown as a warning, not a failure. |
| `IGNORED`       | A matching rule says skip gating entirely. Listed for visibility.                |

The panel printed to stderr surfaces all four buckets; the regressed list drives the exit code.

## A typical CI workflow

```yaml
- name: Restore baseline
  uses: actions/cache@v4
  with: { path: baseline.json, key: bench-baseline-${{ github.base_ref }} }

- name: Run benchmarks
  run: mew run --min-time 1s -o head.json

- name: Gate on regressions
  run: mew compare --fail-on-regression 5 baseline.json head.json
```

Then, persist `head.json` (e.g. via `actions/cache` keyed on the merged SHA) so the next run on `main` becomes the next baseline.

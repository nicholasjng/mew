# Gate pull requests on regressions

This recipe measures a pull request against its base branch on every push and fails the check when a benchmark slows down past a threshold.
It measures both sides in one job, so both run on the same machine, and alternates them to spread drift over both.

## Choose a runner

Shared CI runners share hardware with other jobs and commonly vary by 10–20%, which swamps most real regressions.
Use a dedicated or self-hosted runner, and serialize benchmark jobs so they never compete for it.
Before enabling the gate, run the job a few times on an unchanged branch to see how far the deltas move by themselves.
Pick a threshold above that.

## Measure both sides

Check out the base branch in a worktree next to the pull request, with an environment per side:

```bash
git worktree add ../base "origin/$BASE_REF"
(cd ../base && uv sync --all-extras)
uv sync --all-extras

for i in 1 2 3; do
    (cd ../base && uv run mew run --repetitions 5 --random-interleaving --run-name base --append -o "$OLDPWD/base.jsonl")
    uv run mew run --repetitions 5 --random-interleaving --run-name head --append -o head.jsonl
done
```

Each file pools its three runs as 15 repetitions.
Both sides run the benchmark files of their own checkout.
Benchmarks that only one side has are left out of the comparison, so a renamed benchmark is not gated until it reaches the base branch.

## Gate

```console
$ uv run mew compare --regression-threshold 5% --exit-non-zero-on-regression base.jsonl head.jsonl
```

| Exit code | Meaning | What the job should do |
| --- | --- | --- |
| `0` | Every shared benchmark is within its threshold. | Pass. |
| `1` | The files share no benchmark. | Fail, since the gate checked nothing. |
| `2` | A bad flag, configuration, or result file. | Fail and read stderr. |
| `3` | A benchmark regressed. | Fail and read the verdict panel. |

The gate compares medians against the threshold.
Significance markers and noise warnings show in the table but do not change the verdict, so read a flagged `±N% (!)` before trusting a failure.
Exempt or loosen known-noisy benchmarks with the [allowlist](../guide/comparing.md#allowlist), with a reason for each rule.

## GitHub Actions

```yaml
name: benchmarks

on:
  pull_request:
    paths: ["src/**", "benchmarks/**"]

concurrency:
  group: benchmarks
  cancel-in-progress: false

jobs:
  bench:
    runs-on: [self-hosted, bench]
    steps:
      - uses: actions/checkout@v4
        with: { fetch-depth: 0 }
      - uses: astral-sh/setup-uv@v6
      - name: Measure
        env: { BASE_REF: "${{ github.base_ref }}" }
        run: |
          git worktree add ../base "origin/$BASE_REF"
          (cd ../base && uv sync --all-extras)
          uv sync --all-extras
          for i in 1 2 3; do
            (cd ../base && uv run mew run --repetitions 5 --random-interleaving --run-name base --append -o "$OLDPWD/base.jsonl")
            uv run mew run --repetitions 5 --random-interleaving --run-name head --append -o head.jsonl
          done
      - name: Gate
        run: uv run mew compare --regression-threshold 5% --exit-non-zero-on-regression base.jsonl head.jsonl 2>&1 | tee compare.txt; exit "${PIPESTATUS[0]}"
        shell: bash
      - uses: actions/upload-artifact@v4
        if: always()
        with:
          name: benchmarks
          path: |
            base.jsonl
            head.jsonl
            compare.txt
```

The single concurrency group queues benchmark jobs instead of running them side by side.
`if: always()` keeps the results of a failing gate for inspection.
Set `COLUMNS=120` in the job's environment for a wider table in the log.

## When the gate fails

Rerun the job once.
A regression that does not reproduce points to the runner, and the context table and skew warnings above the table say whether the two sides differed in machine or interpreter.
For one that reproduces, [profile both sides](../howto/ab-comparison.md#profile-both-sides) to find the cause.
For a slowdown that crept in over many merges, use the [Git history recipe](git-history.md) to find the commit.

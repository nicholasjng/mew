# Comparing and gating

`mew compare` reads JSON, JSONL, and gzipped JSONL result files.
The first file is the baseline and the second the candidate.
Files after them are informational.

```console
$ mew compare baseline.json candidate.json
$ mew compare --metric cpu_time -k sort baseline.json candidate.json
```

Above the table, a context table gives each file's run name, the date of its newest run, and how many runs it pools.
It has a line per file, or a line per field when that is too wide.
`--show-context` adds context fields by path, such as `--show-context vcs.commit`, or by glob, such as `jax.*`.
A path also selects every field below it, so `--show-context python` shows the interpreter's version and build.
Named fields show even where every file has the same value.
They are opt-in because a small change, such as a new commit, changes many values at once.
Below it, a line sums up the candidate against the baseline: how many benchmarks got significantly slower or faster.
[Trust the numbers](writing-benchmarks.md#trust-the-numbers) explains the spread and the significance markers.

### Layouts

`mew compare` picks the widest layout that fits the terminal, and `--layout` chooses one:

| Layout | Shows |
| --- | --- |
| `full` | Each file's value, delta and speedup |
| `compact` | The baseline's values, then each file's delta in one cell, marked `▲` or `▼` where significant and dim where not |
| `long` | A line per file under each benchmark, with the full layout's figures |

The full layout costs about 37 columns a file, the compact one about 9.
`--speedup` shows the speedup instead of the delta in the compact layout.
Pass `--layout` in CI logs, whose width differs between runners.

## Save results

`mew run -o` writes a result file and repeats for several destinations:

| Destination | Format |
| --- | --- |
| `-` | The terminal table, or `--format json` or `jsonl` |
| `*.json` | One run as a `{"benchmarks": [...]}` document |
| `*.jsonl`, `*.jsonl.gz` | One self-contained row per line, appendable with `--append` |

```console
$ mew run --repetitions 10 -o - -o results.json
$ mew run --run-name nightly --context runner=ci-3 --append -o history.jsonl.gz
```

`--run-name` labels the run in `mew log`, `mew show`, and `mew compare`.
`--context path=value` records a string in the run's context, over the [setup file's `context()` hook](../reference/configuration.md#setup-file).
A dot nests, so `--context ci.job=42` sets `job` inside `ci` and keeps the hook's other `ci` keys.
`mew show FILE` prints every run in a file as `mew run` printed it, and `mew log FILE` lists the runs, newest first.

A file is one sample: the runs `--append` added to it pool as repetitions.
`mew compare` warns when pooled runs differ in machine, interpreter, mew version, or commit.
To compare runs against each other, write each to its own file, or extract runs from an archive as in the [archive recipe](../recipes/analyzing-results.md#select-runs).

(select-rows)=
### Select rows

`mew log`, `mew show`, and `mew compare` take `--where <path>=<glob>` to keep only the rows whose value at a path matches a glob:

```console
$ mew log history.jsonl.gz --where run.name=nightly
$ mew show history.jsonl.gz --where 'context.vcs.commit=abc12*'
$ mew compare base.jsonl head.jsonl --where context.python.version='3.15.*'
```

Paths start at the row, as in the [result format](../reference/results.md): `run.name`, `context.vcs.commit`, or `params.n`.
A dot nests, and a key that holds a dot is quoted, as in DuckDB: `context."a.b"`.
A string matches as it is, and a number, boolean, or null as JSON writes it, such as `true`.
A missing path, a list, or a table matches nothing.
Repeat `--where` for conditions that must all hold.
In `mew compare`, the conditions apply to every file, and only the matching runs pool.
No matching row in a file exits with code 1.
For anything beyond matching values, query the archive with [SQL](../recipes/analyzing-results.md#summarize-a-history-with-sql).

## Metrics

The default metric is `real_time`.
`cpu_time` is also available, and files recorded with `--memory-profiler` add `memory.peak_bytes` and `memory.allocations_per_iteration`.
Lower is better for all of them.
Compare per-iteration allocations, since the total depends on how many iterations the memory pass ran.

(counters)=
### Counters

`counters.<name>` compares a counter that benchmarks set:

```console
$ mew compare -m counters.items_per_second baseline.json candidate.json
```

`items_per_second` and `bytes_per_second` are higher-is-better.
Give other counters a direction in `pyproject.toml`, where the first matching rule applies:

```toml
[[tool.mew.counters]]
pattern = "*_misses"
higher-is-better = false
```

A counter without a direction compares as a plain ratio, and gating on it is an error.
Benchmarks without the counter are left out.

## Matching

Rows match by their full name, `file.py::func[label]`, relative to the project root, with `/threads:N` for threaded runs.
Timing options do not affect identity.
To compare two implementations, run one benchmark file against each, as in the [A/B how-to](../howto/ab-comparison.md).
Only shared names are compared, so a passing gate does not prove complete coverage.
mew warns about benchmarks a file has as skipped.

## Gate on regressions

```console
$ mew compare --regression-threshold 5% --exit-non-zero-on-regression baseline.json candidate.json
```

The `%` suffix is required.
A threshold prints a verdict panel to stderr, and the exit flag returns code 3 when a benchmark regresses.
The threshold defaults to 5%, configuration overrides that, and the flag overrides both.
The gate uses only the median delta and the threshold, not significance markers or noise warnings.
The [CI recipe](../recipes/ci-gate.md) sets up a gate end to end.

(allowlist)=
### Allowlist

Keep expected drift in `pyproject.toml`:

```toml
[tool.mew.regressions]
default-threshold = 5.0

[[tool.mew.regressions.allow]]
pattern = "benchmarks/bench_io.py::*"
threshold = 15.0
reason = "I/O varies more on this runner."

[[tool.mew.regressions.allow]]
pattern = "*[dataset=remote]"
threshold = inf
reason = "Network timing is tracked separately."
```

Patterns match benchmark names with `*` and `?` wildcards, and brackets match literally.
Each rule needs a `reason` and a `threshold` in percent, looser, stricter, or `inf` to exempt matching benchmarks.

| Verdict | Meaning |
| --- | --- |
| `OK` | Within the active threshold. |
| `REGRESSED` | Over the active threshold, which fails the gate. |
| `ALLOWED_OVER` | Over the default, but a rule accepts it. |

(compare-from-python)=
## Compare from Python

Pass a {class}`~mew.regressions.RegressionConfig` to {func}`~mew.compare.compare`:

```python
from mew.compare import compare
from mew.regressions import RegressionConfig

status = compare(
    ["baseline.json", "candidate.json"],
    regressions=RegressionConfig(default_threshold=5.0),
)
raise SystemExit(status)
```

`compare()` returns `0` for a passing comparison, `1` when the files share no benchmark, and `3` for a regression.
File and argument errors raise exceptions.

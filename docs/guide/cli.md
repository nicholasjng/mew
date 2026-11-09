# Command-line interface

For the generated help reference, see [](../reference/cli.md).

## `mew run`

Discover and run benchmarks.

```console
$ mew run                                  # all benchmarks under benchpaths
$ mew run benchmarks/bench_sort.py         # one file
$ mew run -k 'sort' -t hot-path            # filter by name + tag (OR within tag)
$ mew run --min-time 1s --repetitions 5    # variance-friendly run
$ mew run -o results.json -o -             # JSON file + Rich console
$ mew run --repetitions 5 --random-interleaving   # decorrelate repeats from drift
$ mew run --repetitions 5 --summary-only   # each benchmark's median, not each repetition
```

### Name filters

`-k`/`--pattern` is a regular expression searched for in benchmark names, like
Google Benchmark's `--benchmark_filter`. A match on some cases of a family runs
only those cases. A name as `mew list` shows it ends in brackets, which a regex
reads as a character class, so add `-F`/`--literal` to match it as plain text:

```console
$ mew run -k 'sort|search'                 # a regex
$ mew run -F -k 'bench_sort[n=1000]'       # one case of a family, as listed
```

`-F` applies to `::<filter>` selectors too.
`mew compare -k` and {meth}`mew.Registry.filter` match the same way.

### Path selectors

Positional path args can be plain paths or `<path>::<filter>` selectors, pytest-style:

```console
$ mew run 'benchmarks/bench_sort.py::n=1000'
```

The `::<filter>` portion is a regular expression matched against benchmark names
within that path, or plain text with `-F`. Selectors are combined with OR;
the global `-k` further narrows the selection with AND.
An unfiltered path selects all benchmarks within it.

### Output sinks

Pass `-o` (repeatable):

- `-`: terminal output, formatted by `--format`.
- `*.json`: one JSON document per run.
- `*.jsonl`: one self-contained row per line.
- `*.jsonl.gz`: same rows, gzip-compressed (appends add a new gzip member).

Duplicate sinks (two stdout sinks, or two writers pointing at the same path) are an error.

### stdout format

`--format` sets the format of stdout output: `rich` (default table), `json`, or
`jsonl`. Use `json`/`jsonl` to pipe machine-readable rows downstream; mew's own
messages go to stderr, so stdout stays clean:

```console
$ mew run --format jsonl | jq 'select(.name) | {name, real_time}'
$ mew run --format json | jq '.benchmarks | length'
```

`--format` only configures stdout; file `-o` sinks keep their by-extension format.

### Benchmark output

What benchmarked code writes to stdout or stderr, such as a library's warning on
first use, is captured during the run, native writes included, and printed to
stderr after the run under `output from benchmarks:`. It can neither break up the
table nor corrupt JSON on stdout. `--capture=no` shows it live instead, e.g. when
debugging a crash: captured output is lost if the process dies.

`--append` adds the run to an existing `.jsonl[.gz]` sink instead of overwriting (not supported for `.json`). Combined with `--run-name`, this collects several runs in one file that `mew compare` can then address individually; see [](regressions.md#comparing-runs-in-one-file). `latest` is reserved and can't be a run name: `path@latest` always selects the newest run.

`--context <key>=<value>` records a string in the run's context, over what the setup file's `context()` returns, and repeats; see [](context.md#the-context-hook).

### Selecting from stdin

`--stdin` reads literal, newline-delimited selectors. This supports piping a
filtered `mew list` directly into `mew run`:

```console
$ mew list -k slow | mew run --stdin                    # file.py::name selectors
$ mew list --show-cases -k 'n=1000' | mew run --stdin   # one case; no -F needed
```

Each line is a selector, like a positional path: `file.py::name`, the default
`mew list` output, imports that path and filters by the name. The path is
relative, so run from the directory you listed from. (Don't pipe `--show-tags`
output: a line with a trailing `[tags]` column isn't a selector.)

### Profiling flags

These attach **in-process** measurements to the timing table. For native (C/C++)
frames, sample the process from outside: see {doc}`profiling-native`.

| Flag                     | Effect                                                                      |
| ------------------------ | --------------------------------------------------------------------------- |
| `--cpu-profiler NAME`    | Profile CPU time with a preset: `sample`, one of yours, or a class spec.    |
| `--memory-profiler NAME` | Profile memory with a preset: `memray`, one of yours, or a class spec.      |
| `--profile-dir DIR`      | Write the profilers' reports to `DIR/cpu` and `DIR/memory`.                 |

Each flag takes mew's built-in preset, `sample` for CPU sampling
({doc}`profiling-cpu`) or `memray` for allocations ({doc}`profiling-memory`),
a preset from `[tool.mew.profilers]` ({doc}`configuration`), or a class as
`module:Class` or `path.py:Class`, run with its defaults. Google Benchmark runs
one CPU profiler and one memory profiler per run, so each flag takes one value;
pass both to profile CPU and memory in one run. A class given to the wrong flag
stops the run with exit code 2, naming the flag it fits.

`--profile-dir` needs either flag. With `sample`, `DIR/cpu/index.html` is
a Tachyon flame graph or a pyinstrument call tree; with `memray`,
`DIR/memory/` holds an allocation flame graph per benchmark.
For a cross-engine memory/CPU comparison, run each suite separately with its own artifact paths and compare the result files; see {doc}`ab-comparison`.

## `mew list` (alias `ls`)

Same filters as `mew run`, prints names instead of running:

```console
$ mew ls                       # all
$ mew ls -t sort               # tagged
$ mew ls --show-tags           # `name\t[tag1,tag2]`
```

Exits with code 1 if nothing matches (see [](#exit-codes)).

## `mew compare`

Compare two or more result files (`.json`, `.jsonl`, or `.jsonl.gz`). The first is the baseline; later files are diffed against it.

```console
$ mew compare baseline.json head.json
$ mew compare --metric cpu_time --pattern 'sort' a.json b.json
$ mew compare --regression-threshold 5% --exit-non-zero-on-regression baseline.json head.json
```

See [](regressions.md) for matching, metrics, the regression gate, and allowlist.
With `--exit-non-zero-on-regression`, a regression exits with code 3.

## `mew show`

Print what a result file holds as `mew run` printed it: the newest run by
default, or the newest run of the name a `@<name>` selector gives. Skipped benchmarks show with
their reason, and repeated ones get the closing summary.

```console
$ mew show results.jsonl
$ mew show results.jsonl@before -k sort
$ mew show --summary-only results.jsonl
```

`-k/--pattern` and `-F/--literal` filter as in `mew compare`; a filter that
matches nothing exits with code 1. The `Peak Mem` column and the CPU profile
lines appear when the rows have them. With several files, each prints after
its name.

## `mew log`

List the runs a result file holds, newest first, to pick a `@<name>` selector
for `mew compare` or `mew show`:

```console
$ mew log results.jsonl
Id       │ Date                │ Name   │ Benchmarks │ Rows
───────────────────────────────────────────────────────────
01a0c787 │ 2026-09-22T05:12:29 │ after  │          7 │   63
01a0c786 │ 2026-09-22T05:12:28 │ before │          7 │   63
```

## `mew completions`

Print a shell-completion script for `bash`, `zsh`, or `fish` to
stdout. They complete commands, options, paths, and fixed choices.

The scripts are static and never call `mew` during completion. Install them as
files:

```console
# bash
$ mew completions bash > ~/.local/share/bash-completion/completions/mew

# zsh: write a file, then `source ~/.mew-completions.zsh` in ~/.zshrc after compinit
$ mew completions zsh > ~/.mew-completions.zsh

# fish
$ mew completions fish > ~/.config/fish/completions/mew.fish
```

To generate completions at shell startup, guard against a missing command:

```console
$ command -v mew >/dev/null 2>&1 && eval "$(mew completions zsh)"
```

Because completion never runs the suite, it won't complete benchmark *names*.
Pipe `mew list` if you want those.

(exit-codes)=
## Exit codes

Every command exits with one of these codes, so a script or CI job can tell the
outcomes apart:

| Code | Meaning                                                                                                  |
| ---- | -------------------------------------------------------------------------------------------------------- |
| `0`  | Success.                                                                                                 |
| `1`  | Nothing matched: `mew run` or `mew list` selected no benchmark, `mew show`'s filter matched none, or `mew compare`'s candidate and baseline share none. |
| `2`  | Usage or environment error, explained on stderr: a bad flag, filter or `[tool.mew]` config, a benchmark or setup file that raises on import (after its traceback), a missing or malformed result file, an unknown `@<tag>`, a profiler that can't run here, or `--strict` with threaded benchmarks on a GIL build. |
| `3`  | `mew compare --exit-non-zero-on-regression` found a regression.                                          |

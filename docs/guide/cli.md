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

- `-` or `stdout`: terminal output, formatted by `--format`.
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
debugging a crash: captured output is lost if the process dies. To make that the
project default, set `capture = "no"` in [`[tool.mew]`](configuration.md).

`--append` adds the run as a new session to an existing `.jsonl[.gz]` sink instead of overwriting (not supported for `.json`). Combined with `--session-tag`, this collects several runs in one file that `mew compare` can then address individually; see [](regressions.md#comparing-sessions-in-one-file). `latest` is reserved and can't be a session tag: `path@latest` always selects the newest session.

### Selecting from stdin

`--stdin` reads literal, newline-delimited selectors. This supports piping a
filtered `mew list` directly into `mew run`:

```console
$ mew list -k slow | mew run --stdin                    # file.py::name selectors
$ mew list --show-cases -k 'n=1000' | mew run --stdin   # one case; no -F needed
```

- A line **with `::`** (`file.py::name`, the default `mew list` output) is a
  selector: `mew run` imports that path and filters by the name. The path is
  relative, so run from the directory you listed from.
- A **path-free** line (`mew list --names-only` output) filters benchmarks
  discovered from positional paths or `[tool.mew] benchpaths`:

```console
$ mew list --names-only -k slow | mew run benchmarks/ --stdin
$ mew list --names-only | mew run --stdin       # discovery via benchpaths
```

A path-free name matches by substring, so a function name shared across files
selects all of them. (Don't pipe `--show-tags` output; the trailing `[tags]`
column isn't a selector.)

### Profiling flags

These attach **in-process** measurements to the timing table. For native (C/C++)
frames, sample the process from outside: see {doc}`profiling-native`.

| Flag                    | Effect                                                                      |
| ----------------------- | --------------------------------------------------------------------------- |
| `--sample`              | Sample each benchmark in-process with the `--sampler`.                      |
| `--sampler NAME`        | `tachyon` (3.15+), `pyinstrument`, or `auto`: Tachyon on 3.15+, else pyinstrument (default). |
| `--sample-html FILE`    | Write an HTML CPU profile: a Tachyon flame graph or a pyinstrument call tree. |
| `--sample-interval F`   | Sampling interval seconds (default `1e-4`).                                 |
| `--sample-top-k K`      | Hottest bytecode instructions per row with Tachyon on free-threaded 3.15+ (default 5). |
| `--profile-memory`      | Run each benchmark under `memray`.                                          |
| `--memory-iterations N` | Cap the memory pass at `N` iterations (default 16).                         |
| `--flamegraph FILE`     | Write an HTML flame graph with allocation data.                             |

`--sample-html`/`--flamegraph` imply `--sample` / `--profile-memory` respectively.
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

Compare two or more result files (`.json`, `.jsonl`, or `.jsonl.gz`). The last is the baseline; earlier files are diffed against it.

```console
$ mew compare head.json baseline.json
$ mew compare --metric cpu_time --pattern 'sort' a.json b.json
$ mew compare --key func suite_a.jsonl suite_b.jsonl   # match by function name
$ mew compare --regression-threshold 5% --exit-non-zero-on-regression head.json baseline.json
```

See [](regressions.md) for matching, metrics, the regression gate, and allowlist.
With `--exit-non-zero-on-regression`, a regression exits with code 3.

## `mew sessions`

List what a result file holds, newest session first, to pick a `@<tag>`
selector for `mew compare`:

```console
$ mew sessions results.jsonl
Id       │ Date                │ Host   │ Tag    │ Benchmarks │ Rows
──────────────────────────────────────────────────────────────────────
01a0c787 │ 2026-09-22T05:12:29 │ laptop │ after  │          7 │   63
01a0c786 │ 2026-09-22T05:12:28 │ laptop │ before │          7 │   63
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
Pipe `mew list --names-only` if you want those.

(exit-codes)=
## Exit codes

Every command exits with one of these codes, so a script or CI job can tell the
outcomes apart:

| Code | Meaning                                                                                                  |
| ---- | -------------------------------------------------------------------------------------------------------- |
| `0`  | Success.                                                                                                 |
| `1`  | Nothing matched: `mew run` or `mew list` selected no benchmark, or `mew compare`'s candidate and baseline share none. |
| `2`  | Usage or environment error, explained on stderr: a bad flag, filter or `[tool.mew]` config, a benchmark or setup file that raises on import (after its traceback), a missing or malformed result file, an unknown `@<tag>`, a profiler that can't run here, or `--strict` with threaded benchmarks on a GIL build. |
| `3`  | `mew compare --exit-non-zero-on-regression` found a regression.                                          |

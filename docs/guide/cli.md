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
```

### Path selectors

Positional path args can be plain paths or `<path>::<filter>` selectors, pytest-style:

```console
$ mew run 'benchmarks/bench_sort.py::n=1000'
```

The `::<filter>` portion is a regular expression matched against benchmark names
within that path. Selectors are combined with OR; the global `-k` further narrows
the selection with AND. An unfiltered path selects all benchmarks within it.

### Output sinks

Pass `-o` (repeatable):

- `-` or `stdout`: terminal output, formatted by `--format`.
- `*.json`: Google Benchmark-shaped JSON document.
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

`--append` adds the run as a new session to an existing `.jsonl[.gz]` sink instead of overwriting (not supported for `.json`). Combined with `--session-tag`, this collects several runs in one file that `mew compare` can then address individually; see [](regressions.md#comparing-sessions-in-one-file).

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

| Flag                    | Effect                                              |
| ----------------------- | --------------------------------------------------- |
| `--sample`              | Sample each benchmark in-process with `pyinstrument`. |
| `--sample-html FILE`    | Write an HTML pyinstrument report.                  |
| `--sample-interval F`   | Sampling interval seconds (default `1e-4`).         |
| `--profile-memory`      | Run each benchmark under `memray`.                  |
| `--flamegraph FILE`     | Write an HTML flame graph with allocation data.     |

`--sample-html`/`--flamegraph` imply `--sample` / `--profile-memory` respectively.
For a cross-engine memory/CPU comparison, run each suite separately with its own artifact paths and pivot the merged file; see {doc}`ab-comparison`.

## `mew list` (alias `ls`)

Same filters as `mew run`, prints names instead of running:

```console
$ mew ls                       # all
$ mew ls -t sort               # tagged
$ mew ls --show-tags           # `name\t[tag1,tag2]`
```

Exit code `1` if nothing matches.

## `mew compare`

Compare two or more result files (`.json`, `.jsonl`, or `.jsonl.gz`). The last is the baseline; earlier files are diffed against it.

```console
$ mew compare head.json baseline.json
$ mew compare --metric cpu_time --pattern 'sort' a.json b.json
$ mew compare --key func suite_a.jsonl suite_b.jsonl   # match by function name
$ mew compare --regression-threshold 5% --exit-non-zero-on-regression head.json baseline.json
```

See [](regressions.md) for matching, metrics, the regression gate, and allowlist.

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

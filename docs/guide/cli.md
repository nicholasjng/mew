# Command-line interface

The `mew` CLI has three commands. For a generated help reference, see [](../reference/cli.md).

## `mew run`

Discover and run benchmarks.

```console
$ mew run                                  # all benchmarks under benchpaths
$ mew run benchmarks/bench_sort.py         # one file
$ mew run -k 'sort' -t hot-path            # filter by name + tag (OR within tag)
$ mew run --min-time 1s --repetitions 5    # variance-friendly run
$ mew run -o results.json -o -             # JSON file + Rich console
$ mew run --benchmark-option=--benchmark_color=true   # raw GB passthrough
```

### Path selectors

Positional path args can be plain paths or `<path>::<filter>` selectors, pytest-style:

```console
$ mew run 'benchmarks/bench_sort.py::n=1000'
```

The `::<filter>` portion is a substring match against the registered benchmark name.
Per-selector filters are OR'd with the global `-k`.

### Output sinks

Pass `-o` (repeatable):

- `-` or `stdout` — terminal output, formatted by `--format`.
- `*.json` — Google Benchmark-shaped JSON document.
- `*.parquet` or `*.pq` — one row per Run.

Duplicate sinks (two stdout sinks, or two writers pointing at the same path) are an error.

### stdout format

`--format` sets the format of stdout output: `rich` (default table), `json`, or
`jsonl`. Use `json`/`jsonl` to pipe machine-readable rows downstream — mew's own
messages go to stderr, so stdout stays clean:

```console
$ mew run --format jsonl | jq 'select(.name) | {name, real_time}'
$ mew run --format json | jq '.benchmarks | length'
```

`--format` only configures stdout; file `-o` sinks keep their by-extension
format. (It mirrors Google Benchmark's `--benchmark_format`, which sets the
console format while `--benchmark_out` handles files.)

`--append` adds the run as a new session to an existing `.jsonl` / `.parquet` sink instead of overwriting (not supported for `.json`). Combined with `--session-tag`, this collects several runs in one file that `mew compare` can then address individually — see [](regressions.md#comparing-sessions-in-one-file).

### Selecting from stdin

`--stdin` reads newline-delimited selectors from standard input, so you can pipe
a filtered `mew list` straight into a run — no `xargs`. Each line is matched
**literally**, so a displayed `name[label]` (brackets and all) works without
escaping. Lines come in two shapes:

```console
$ mew list -k slow | mew run --stdin                    # file.py::name selectors
$ mew list --show-cases -k 'n=1000' | mew run --stdin   # one case; no -F needed
```

- A line **with `::`** (`file.py::name`, the default `mew list` output) is a
  selector: `mew run` imports that path and filters by the name. The path is
  relative, so run from the directory you listed from.
- A **path-free** line (`mew list --names-only` output, like `docker ps -q`) is
  a name *filter*: `mew run` discovers benchmarks its usual way (positional paths
  or `[tool.mew] benchpaths`) and keeps the ones whose name matches. Because the
  name carries no path, this round-trips from **any** directory:

```console
$ mew list --names-only -k slow | mew run benchmarks/ --stdin
$ mew list --names-only | mew run --stdin       # discovery via benchpaths
```

A path-free name matches by substring, so a function name shared across files
selects all of them. (Don't pipe `--show-tags` output; the trailing `[tags]`
column isn't a selector.)

### Profiling flags

These attach **in-process** measurements to the timing table. For native (C/C++)
frames, use {doc}`mew profile <profiling-native>`.

| Flag                    | Effect                                              |
| ----------------------- | --------------------------------------------------- |
| `--sample`              | Sample each benchmark in-process with `pyinstrument`. |
| `--sample-html FILE`    | Write an HTML pyinstrument report.                  |
| `--sample-interval F`   | Sampling interval seconds (default `1e-4`).         |
| `--sample-iterations N` | Body iterations under the sampler (default `1000`). |
| `--profile-memory`      | Run each benchmark under `memray`.                  |
| `--memory-iterations N` | Measured loop passes per case under `--profile-memory` (default `100`, plus warmup); drives `memory.allocations_per_iteration`. |
| `--flamegraph FILE`     | Write an HTML flame graph with allocation data.     |

`--sample-html`/`--flamegraph` imply `--sample` / `--profile-memory` respectively.
All of these compose with `--variant`: each variant child runs its own profile pass, so cross-engine memory/CPU comparison works from one `mew run`.

## `mew profile`

Profile out-of-process to capture native C frames (which `--sample` can't see).
Picks a backend (`xctrace` (macOS), `py-spy` (Linux/Windows), or `perf` (Linux)), and records an artifact you open in its viewer.

```console
$ mew profile                       # auto-select the platform's native profiler
$ mew profile -p xctrace --open     # record and open in Instruments.app
$ mew profile -k bench_sort         # filter like `mew run`
```

| Flag                 | Effect                                                       |
| -------------------- | ------------------------------------------------------------ |
| `-p, --profiler`     | `auto` (default), `xctrace`, `py-spy`, or `perf`.            |
| `-o, --output-dir`   | Where artifacts land (default `./.mew-traces`).             |
| `--iterations N`     | Body iterations under the sampler (default `100000`).        |
| `--time-limit DUR`   | Hard cap per recording, e.g. `10s`.                          |
| `--template NAME`    | (xctrace) Instruments template; default `Time Profiler`.    |
| `--separate`         | (xctrace) One bundle per case instead of one combined.       |
| `--open`             | Open the artifact(s) in their viewer when done.              |

When `auto` finds no native profiler (e.g. macOS without Xcode), it points you
to `mew run --sample` for in-process Python sampling. See {doc}`profiling-native`.

## `mew list` (alias `ls`)

Same filters as `mew run`, prints names instead of running:

```console
$ mew ls                       # all
$ mew ls -t sort               # tagged
$ mew ls --show-tags           # `name\t[tag1,tag2]`
```

Exit code `1` if nothing matches.

## `mew compare`

Compare two or more result files (`.json`, `.jsonl`, or `.parquet`). The first is the baseline; subsequent files are diffed against it.

```console
$ mew compare baseline.json head.json
$ mew compare --metric cpu_time --pattern 'sort' a.json b.json
$ mew compare --key func suite_a.jsonl suite_b.jsonl   # match by function name
$ mew compare --fail-on-regression 5 baseline.json head.json
```

See [](regressions.md) for matching, metrics, the regression gate, and allowlist.

## `mew completions`

Print a shell-completion script for `bash`, `zsh`, `fish`, or `powershell` to
stdout. The scripts are generated from the CLI itself, so they stay in sync with
the commands and flags. They complete subcommands, per-command options, file
paths for path arguments, and fixed choices (`--format`, `--profiler`, the shell
list).

The generated scripts are **static** — they never call `mew` at completion time.
So prefer installing them as a **file**, generated once: shell startup then has
no dependency on `mew` being importable, which matters when `mew` lives only in a
virtualenv (e.g. Homebrew Python, where you can't install into the interpreter).

```console
# bash
$ mew completions bash > ~/.local/share/bash-completion/completions/mew

# zsh — write a file, then `source ~/.mew-completions.zsh` in ~/.zshrc after compinit
$ mew completions zsh > ~/.mew-completions.zsh

# fish
$ mew completions fish > ~/.config/fish/completions/mew.fish

# PowerShell — dot-source the file from $PROFILE
$ mew completions powershell > ~/.mew-completions.ps1
```

The `eval` one-liner (`eval "$(mew completions zsh)"` in your rc) also works, but
it re-runs `mew` at every shell startup — so **guard it**, or you'll get a
`command not found: mew` on every new shell when no venv is active:

```console
$ command -v mew >/dev/null 2>&1 && eval "$(mew completions zsh)"
```

Completion is static — it does not run the suite, so it won't complete benchmark
*names*. Pipe `mew list --names-only` if you want those.

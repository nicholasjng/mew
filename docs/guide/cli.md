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

- `-` or `stdout` — Rich terminal table.
- `*.json` — Google Benchmark-shaped JSON document.
- `*.parquet` or `*.pq` — one row per Run.

Duplicate sinks (two stdout sinks, or two writers pointing at the same path) are an error.

### Profiling flags

These attach **in-process** measurements to the timing table. For native (C/C++)
frames, use {doc}`mew profile <profiling-native>` instead.

| Flag                    | Effect                                              |
| ----------------------- | --------------------------------------------------- |
| `--sample`              | Sample each benchmark in-process with `pyinstrument`. |
| `--sample-html FILE`    | Write an HTML pyinstrument report.                  |
| `--sample-interval F`   | Sampling interval seconds (default `1e-4`).         |
| `--sample-iterations N` | Body iterations under the sampler (default `1000`). |
| `--profile-memory`      | Run each benchmark under `memray`.                  |
| `--flamegraph FILE`     | Write an HTML flame graph with allocation data.     |

`--sample-html`/`--flamegraph` imply `--sample` / `--profile-memory` respectively.

## `mew profile`

Profile out-of-process to capture native C frames (which `--sample` can't see).
Picks a native-frame backend — `xctrace` (macOS), `py-spy` (Linux/Windows), or
`perf` (Linux) — and records an artifact you open in its viewer.

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

Compare two or more result files. The first is the baseline; subsequent files are diffed against it.

```console
$ mew compare baseline.json head.json
$ mew compare --metric cpu_time --pattern 'sort' a.json b.json
$ mew compare --fail-on-regression 5 baseline.json head.json
```

See [](regressions.md) for the regression gate and allowlist.

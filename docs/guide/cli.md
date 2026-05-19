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

| Flag                  | Effect                                              |
| --------------------- | --------------------------------------------------- |
| `--profile-cpu`       | Run each benchmark under `pyinstrument` once.       |
| `--cpu-output FILE`   | Write an HTML pyinstrument report.                  |
| `--cpu-interval F`    | Sampling interval seconds (default `1e-4`).         |
| `--cpu-iterations N`  | Body iterations under the sampler (default `1000`). |
| `--profile-memory`    | Run each benchmark under `memray`.                  |
| `--flamegraph FILE`   | Write an HTML flame graph with allocation data.     |

`--cpu-output`/`--flamegraph` imply their respective `--profile-*` flag.

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

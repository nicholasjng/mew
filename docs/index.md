# Introduction

> Simple microbenchmarking of Python snippets, powered by [Google Benchmark](https://github.com/google/benchmark).

`mew` is a small Python library and CLI for writing microbenchmarks the way you
write tests: decorate a function, run `mew`, get reliable timings — backed by
Google Benchmark, built with nanobind.

```python
import mew


@mew.benchmark
def bench_sorted(state: mew.State) -> None:
    data = list(range(1000, 0, -1))
    for _ in state:
        sorted(data)
```

```console
$ mew run
mew · host=laptop cpus=10 @ 3200MHz scaling=enabled
Benchmark                              │       Iters │           Real │            CPU
─────────────────────────────────────────────────────────────────────────────────────
benchmarks/bench_sort.py::bench_sorted │   1,000,000 │      32.10 ns │      32.05 ns
```

## At a glance

::::{grid} 2
:gutter: 3

:::{grid-item-card} Decorate
:link: guide/writing-benchmarks
:link-type: doc
`@mew.benchmark`, `@mew.parametrize`, `@mew.product` — register one
benchmark or a family.
:::

:::{grid-item-card} Run
:link: guide/cli
:link-type: doc
`mew run` discovers `bench_*.py` files, runs them with Google Benchmark, and
streams results to the terminal, JSON, or Parquet.
:::

:::{grid-item-card} Profile
:link: guide/profiling-cpu
:link-type: doc
`--sample` for `pyinstrument` CPU sampling, `--profile-memory` for `memray`
allocations, or `mew profile` for native C frames via Instruments / py-spy / perf.
:::

:::{grid-item-card} Compare
:link: guide/regressions
:link-type: doc
`mew compare baseline.json head.json --fail-on-regression 5` for CI
regression gates with an allowlist.
:::

::::

## Table of Contents

```{toctree}
:caption: Getting started
:maxdepth: 1

getting-started/installation
getting-started/quickstart
getting-started/concepts
```

```{toctree}
:caption: User guide
:maxdepth: 1

guide/writing-benchmarks
guide/parametrize-product
guide/state-and-timing
guide/context
guide/configuration
guide/cli
guide/variants
guide/reporters
guide/profiling-cpu
guide/profiling-memory
guide/profiling-native
guide/regressions
```

```{toctree}
:caption: Reference
:maxdepth: 1

reference/api/index
reference/cli
```

```{toctree}
:caption: Development
:maxdepth: 1

development/building
development/contributing
```

```{toctree}
:hidden:

changelog
```

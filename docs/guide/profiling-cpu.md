# CPU profiling

`--profile-cpu` runs each selected benchmark under [pyinstrument](https://pyinstrument.readthedocs.io/) once, separately from the timing pass, and attaches a summary, including sample count and hottest frame, to each run.

## Prerequisites

```console
$ uv add 'mew[cpu]'    # or: pip install 'mew[cpu]'
```

## Basics

```console
$ mew run --profile-cpu
mew · host=laptop cpus=10 …
Benchmark         │   Iters │   Real │ Samples │ Hottest Frame
─────────────────────────────────────────────────────────────────
bench_sort_quick  │ 100,000 │ 24 µs  │  12,341 │ _quicksort (bench.py:18)
```

Write a self-contained HTML report:

```console
$ mew run --profile-cpu --cpu-output cpu.html
```

This implies `--profile-cpu`, so you can drop one flag:

```console
$ mew run --cpu-output cpu.html
```

## Tuning the sampler

| Flag                  | Default | Notes                                                   |
| --------------------- | ------- | ------------------------------------------------------- |
| `--cpu-interval F`    | `1e-4`  | Smaller = more samples = higher overhead.               |
| `--cpu-iterations N`  | `1000`  | How many times the body runs under the sampler.         |

A profiling pass is **separate** from the timing pass.
Profiler overhead does not pollute timing numbers, but the profiled iteration count is independent of `min_time`,
so don't read timings out of the profiling report.

## What you'll see

`cpu.html` is a single self-contained pyinstrument page with a tree view per benchmark.
For the example workloads under `benchmarks/bench_cpu.py`:

- `bench_sort_builtin` collapses into a single wide `sorted` C frame.
- `bench_sort_quicksort` shows deep recursion into `_quicksort`.
- `bench_sort_bubble` shows the flat double-loop hot path.
- `bench_fib_naive` shows the exponential branching of naive recursion.

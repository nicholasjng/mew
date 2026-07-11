# CPU profiling

`--sample` runs each selected benchmark under [pyinstrument](https://pyinstrument.readthedocs.io/) once, separately from the timing pass, and attaches a summary, including sample count and hottest frame, to each run.

This is **in-process** sampling: it sees Python frames only. To capture native
(C/C++) frames from a compiled extension, use {doc}`mew profile <profiling-native>`.

## Prerequisites

```console
$ uv add 'mew-bench[cpu]'    # or: pip install 'mew-bench[cpu]'
```

## Basics

```console
$ mew run --sample
mew · host=laptop cpus=10 …
Benchmark         │   Iters │   Real │ Samples │ Hottest Frame
─────────────────────────────────────────────────────────────────
bench_sort_quick  │ 100,000 │ 24 µs  │  12,341 │ _quicksort (bench.py:18)
```

Write a self-contained HTML report:

```console
$ mew run --sample --sample-html cpu.html
```

This implies `--sample`, so you can drop it:

```console
$ mew run --sample-html cpu.html
```

## Tuning the sampler

| Flag                    | Default | Notes                                                   |
| ----------------------- | ------- | ------------------------------------------------------- |
| `--sample-interval F`   | `1e-4`  | Smaller = more samples = higher overhead.               |
| `--sample-iterations N` | `1000`  | How many times the body runs under the sampler.         |

The profiling pass is **separate** from the timing pass, so profiler overhead doesn't pollute timing numbers.
But the profiled iteration count is independent of `min_time`, so don't read timings out of the profiling report.

## Reading the report

`cpu.html` is a self-contained pyinstrument page with a call tree per benchmark.
Because sampling sees Python frames only, a body that bottoms out in C — `sorted`,
a NumPy call, a compiled extension — collapses into one wide frame with no
detail beneath it. That flat frame is the signal to switch to
{doc}`mew profile <profiling-native>`, which can see inside it.

# Quickstart

## Install

Install the `mew-bench` package in the environment that contains the code you want to measure.
It imports as `mew`, and its command is also `mew`:

```console
$ pip install mew-bench
$ uv add --dev mew-bench        # in a uv project, then prefix commands with `uv run`
```

mew needs Python 3.11 or later.
Supported platforms get a pre-built wheel.
Other platforms compile the C++ extension from source, which needs CMake 3.21 or later and a C++17 compiler.
Extras add the built-in profilers:

| Extra | Pulls in | Enables |
| --- | --- | --- |
| `cpu` | `pyinstrument` | `mew run --cpu-profiler sample` before Python 3.15 |
| `memory` | `memray` (Linux and macOS) | `mew run --memory-profiler memray` |

Check the installation:

```console
$ mew --version
mew 0.3.0 (Google Benchmark v1.10.0-13-g3251a251+mew)
```

The trailing identifier names the Google Benchmark commit the extension was built against, with `+mew` marking [mew's patches](development/patches.md).

## Write a benchmark

Save this as `benchmarks/bench_sort.py`:

```python
import mew


@mew.benchmark
def bench_sorted(state: mew.State) -> None:
    data = list(range(1000, 0, -1))
    for _ in state:
        sorted(data)
```

The input is built before the loop, so each iteration measures sorting an existing list.
`sorted` returns a new list and leaves the input unchanged, so every iteration sorts the same descending values.

## Run it

From the project directory:

```console
$ mew list
$ mew run
```

mew discovers `bench_*.py` and `*_bench.py` files under `benchmarks/` by default.
The table shows the registered name, the iteration count, and wall-clock (`Real`) and CPU time per iteration.
Google Benchmark chooses enough iterations to reach the minimum measurement time, half a second by default.
The reported time includes the Python loop and the call to `sorted`.

## Save and compare results

Collect repetitions so you can see how much the measurements vary:

```console
$ mew run --repetitions 10 -o baseline.json
```

After changing the code under test, measure again on the same machine and compare:

```console
$ mew run --repetitions 10 -o candidate.json
$ mew compare baseline.json candidate.json
```

The first file is the baseline.
The comparison reduces each benchmark's repetitions to their median and shows their spread as `±N%`.
A positive time delta means the candidate is slower.
Read [trusting your numbers](guide/writing-benchmarks.md#trust-the-numbers) before treating a small difference as an improvement.

## Try more inputs

Replace the decorator with a parametrized one, one case per dictionary:

```python
@mew.parametrize([{"n": 10}, {"n": 100}, {"n": 1000}])
def bench_sorted(state: mew.State, n: int) -> None:
    data = list(range(n, 0, -1))
    for _ in state:
        sorted(data)
```

Each case becomes a separate result, such as `bench_sorted[n=1000]`.
`mew list --show-cases` lists them.

Continue with [writing and measuring benchmarks](guide/writing-benchmarks.md), [comparing and gating](guide/comparing.md), or [the recipes](recipes/index.md).

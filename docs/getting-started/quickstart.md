# Quickstart

You'll write one benchmark, run it, and inspect the result.

## 1. Create `benchmarks/bench_sort.py`

```python
# benchmarks/bench_sort.py
import mew


@mew.benchmark
def bench_sorted(state: mew.State) -> None:
    data = list(range(1000, 0, -1))
    for _ in state:
        sorted(data)
```

The contract matches Google Benchmark: setup goes outside the `for _ in state:` loop, measured work inside it.
By default `mew` discovers files matching `bench_*.py` or `*_bench.py` under `benchmarks/`.

## 2. Run it

```console
$ mew run
mew · host=laptop cpus=10 @ 3200MHz scaling=enabled
Benchmark                              │     Iters │       Real │        CPU
────────────────────────────────────────────────────────────────────────────
benchmarks/bench_sort.py::bench_sorted │ 1,000,000 │   32.10 ns │   32.05 ns
```

## 3. Persist results

Direct output to a file with `-o`:

```console
$ mew run -o results.json          # JSON document
$ mew run -o results.parquet       # one row per Run
$ mew run -o - -o results.json     # fan out to stdout AND a file
```

## 4. Parametrize

To benchmark a function across many inputs, decorate it with `@mew.parametrize`:

```python
@mew.parametrize([{"n": 10}, {"n": 100}, {"n": 1000}])
def bench_sorted(state: mew.State, n: int) -> None:
    data = list(range(n, 0, -1))
    for _ in state:
        sorted(data)
```

Or use a cartesian product over parameter axes:

```python
@mew.product(n=[10, 100, 1000], algo=["timsort", "quick"])
def bench_sort(state: mew.State, n: int, algo: str) -> None:
    ...
```

See [](../guide/parametrize-product.md) for full semantics.

## Next steps

- [](concepts.md): how `mew` thinks about benchmarks, families, and timings.
- [](../guide/profiling-cpu.md): turn a slow benchmark into a flame graph.
- [](../guide/regressions.md): fail CI when a benchmark regresses by more than _N_ %.

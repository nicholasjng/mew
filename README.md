# mew

[![Documentation Status](https://readthedocs.org/projects/mew/badge/?version=latest)](https://mew.readthedocs.io/en/latest/)
[![CI](https://github.com/nicholasjng/mew/actions/workflows/ci.yml/badge.svg)](https://github.com/nicholasjng/mew/actions/workflows/ci.yml)

mew measures the cost of Python code using [Google Benchmark](https://github.com/google/benchmark).
Write a benchmark function and run it from Python or the command line.

```python
import mew


@mew.benchmark
def bench_sorted(state: mew.State) -> None:
    data = list(range(1000, 0, -1))
    for _ in state:
        sorted(data)
```

Save this as `benchmarks/bench_sort.py`, then run:

```console
$ mew run --repetitions 10 -o baseline.json
```

Setup before the loop is excluded from timing.
mew chooses an iteration count and reports wall-clock and CPU time per iteration.
After changing the code under test, measure again and compare against the baseline:

```console
$ mew run --repetitions 10 -o candidate.json
$ mew compare --stddev candidate.json baseline.json
```

You can parametrize inputs, run benchmarks from a Python script, and save results as JSON or an appendable JSONL archive.
Optional CPU and memory profilers help investigate changes.

## Installation

```console
$ pip install mew-bench
# Or, in a uv project:
$ uv add --dev mew-bench
```

Requires Python 3.11 or later.
A prebuilt wheel is installed where available; other platforms compile the C++ extension from source.

## Documentation

- [First benchmark](https://mew.readthedocs.io/en/latest/getting-started/quickstart.html)
- [Microbenchmark setup](https://mew.readthedocs.io/en/latest/guide/microbenchmarking.html)
- [Using the Python API](https://mew.readthedocs.io/en/latest/guide/python-api.html)
- [API reference](https://mew.readthedocs.io/en/latest/reference/api/index.html)
- [Recipes](https://mew.readthedocs.io/en/latest/recipes/index.html), including [tracking JAX through Git history and bisecting regressions](https://mew.readthedocs.io/en/latest/recipes/git-history.html)
- [CLI reference](https://mew.readthedocs.io/en/latest/reference/cli.html)

## License

The project license is Apache-2.0.
Bundled third-party code also carries BSD-3-Clause and MIT licenses; see [LICENSE](LICENSE) and [licenses/](licenses/).

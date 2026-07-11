# Parametrizations

Two decorators register benchmark _families_: one body, many variants.

## `@mew.parametrize`

Takes an iterable of keyword argument dicts, registering one variant per dict:

```python
@mew.parametrize(
    [
        {"n": 10, "algo": "merge"},
        {"n": 100, "algo": "quick"},
    ],
    min_time=0.05,
    tags="sort",
)
def bench_sort(state: mew.State, n: int, algo: str) -> None:
    data = list(range(n, 0, -1))
    for _ in state:
        sorted(data)
```

Registered names:

```
benchmarks/bench_sort.py::bench_sort[n=10-algo='merge']
benchmarks/bench_sort.py::bench_sort[n=100-algo='quick']
```

You can override the labels with the `ids` argument:

```python
@mew.parametrize(
    [{"n": 10}, {"n": 1000}],
    ids=["small", "large"],
)
def bench(state, n): ...
```

The length of `ids` **must** equal the number of parametrizations.

## `@mew.product`

For when the variants are a cartesian product of independent axes:

```python
@mew.product(n=[10, 100, 1000], algo=["merge", "quick"], tags="sort")
def bench_sort(state, n, algo): ...
```

This registers six benchmarks.
Use `ids=` to supply a flat list of labels; its length must again equal the product size.

See {func}`mew.parametrize` and {func}`mew.product` for full parameter docs.

## Picking between them

- Reach for `@product` when the axes are independent and you genuinely want every combination.
- Reach for `@parametrize` when only certain pairings are meaningful, e.g. when some algorithms only support certain sizes.
- Need both styles in one file? Define multiple functions; the registry is a flat list and order doesn't matter.

## Filtering at run time

Filter by variant label via the global `-k` pattern (substring match):

```console
$ mew run -k 'algo=quick'
$ mew run -k 'n=1000'
```

Or combine discovery and filtering with the selector form:

```console
$ mew run 'benchmarks/bench_sort.py::n=1000'
```

To see the individual cases a family expands to, list them by label:

```console
$ mew list --show-cases
benchmarks/bench_sort.py::bench_sort[n=100]
benchmarks/bench_sort.py::bench_sort[n=1000]
```

`-k` is a regex, so selecting one case by its displayed `name[label]` would mean
escaping the brackets (`bench_sort\[n=1000\]`). Pass `-F` / `--literal` to match
the pattern as a literal string and paste the name as shown:

```console
$ mew run -F -k 'bench_sort[n=1000]'
```

# Installation

`mew-bench` is distributed as a CPython 3.11+ package with a small C++ extension
(Google Benchmark via nanobind). Install it as `mew-bench`, import it as `mew`
(the shorter name was already taken on PyPI); the CLI is `mew` either way.
On supported platforms a pre-built wheel is
installed; otherwise the C++ extension is compiled from source; see
[](../development/building.md) for the toolchain requirements.

## Using `uv`

```console
$ uv add mew-bench
```

## Using `pip`

```console
$ pip install mew-bench
```

To track the development version instead, point at the repository:

```console
$ uv add mew-bench --git https://github.com/nicholasjng/mew
$ pip install git+https://github.com/nicholasjng/mew.git
```

## Global install with `uv tool`

To make `mew` available system-wide, install it as a [uv tool](https://docs.astral.sh/uv/concepts/tools/):

```console
$ uv tool install mew-bench
```

This drops `mew` into an isolated tool environment that uv keeps on your `PATH`.
Verify with `mew --version`.

That isolation is the catch: the tool environment contains **only** `mew`, not
your benchmark suite's dependencies. So the commands split in two:

- **Work anywhere**: `mew compare` (reads result files), `mew completions`, and
  Tab completion. The completion callbacks read a cached benchmark index and
  never import your `bench_*.py`, so they resolve from outside the project (see
  [](../guide/cli.md#mew-completions)).
- **Need your project's deps**: `mew run`, `mew list`, and `mew profile` import
  your benchmark files. If those import anything beyond the standard library and
  `mew`, a bare tool environment can't resolve them.

For the second group, either pull the extra packages into the tool environment:

```console
$ uv tool install mew-bench --with numpy --with pandas
```

or, usually simpler, run `mew` from the project environment that already has
them, with no global install at all:

```console
$ uv run mew run benchmarks/        # from the project directory; uv syncs first
```

`uv run` resolves the project environment without activation. Activating it
(`source .venv/bin/activate`) puts the same `mew` shim on `PATH` for that shell,
after which a bare `mew run …` works too, but that's scoped to the active
shell, not system-wide.

A good split is a global `uv tool` install for the always-on CLI and shell
completions, plus `uv run mew run` inside each project to actually execute its
benchmarks.

## Optional extras

Extras enabling additional CLI features:

| Extra      | Pulls in                  | Enables                                                  |
| ---------- | ------------------------- | -------------------------------------------------------- |
| `cpu`      | `pyinstrument`            | `mew run --sample`, `--sample-html report.html`          |
| `memory`   | `memray` (non-Windows)    | `mew run --profile-memory`, `--flamegraph alloc.html`    |

Local development uses dependency groups (`build`, `docs`, `test`, `typing`)
rather than extras; see [](../development/contributing.md).

```console
$ uv add 'mew-bench[cpu,memory]'
```

## Verifying

```console
$ mew --version
mew 0.1.0 (Google Benchmark v1.9.5-74-ga8460680)
```

The trailing identifier is `git describe` output for the Google Benchmark commit
the C++ extension was built against.

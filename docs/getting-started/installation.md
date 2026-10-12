# Installation

Install the `mew-bench` package and import it as `mew`; its CLI command is also
`mew`. Supported platforms use a pre-built wheel. Other platforms compile the
C++ extension from source; see
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
$ uv add git+https://github.com/nicholasjng/mew
$ pip install git+https://github.com/nicholasjng/mew.git
```

## Global install with `uv tool`

To make `mew` available system-wide, install it as a [uv tool](https://docs.astral.sh/uv/concepts/tools/):

```console
$ uv tool install mew-bench
```

The tool environment does not contain your benchmark suite's dependencies:

- `mew compare`, `mew sessions` and `mew completions` work from the tool
  environment.
- `mew run` and `mew list` require the dependencies imported by the benchmark
  files.

For the second group, either pull the extra packages into the tool environment:

```console
$ uv tool install mew-bench --with numpy --with pandas
```

Alternatively, run `mew` from the project environment:

```console
$ uv run mew run benchmarks/        # from the project directory; uv syncs first
```

`uv run` resolves the project environment without activation.

## Optional extras

Extras enabling additional CLI features:

| Extra      | Pulls in                  | Enables                                                  |
| ---------- | ------------------------- | -------------------------------------------------------- |
| `cpu`      | `pyinstrument`            | `mew run --sample` before Python 3.15, `--sampler pyinstrument` |
| `memory`   | `memray` (non-Windows)    | `mew run --profile-memory`, `--flamegraph alloc.html`    |

Local development uses dependency groups (`build`, `docs`, `test`) rather than extras;
see [](../development/contributing.md).

```console
$ uv add 'mew-bench[cpu,memory]'
```

## Verifying

```console
$ mew --version
mew 0.2.0 (Google Benchmark v1.9.5-144-ge662de9a+mew)
```

The trailing identifier names the Google Benchmark commit the C++ extension was
built against, in `git describe` form; `+mew` marks it as patched by mew (see
[](../development/patches.md)).

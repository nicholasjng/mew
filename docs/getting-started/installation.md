# Installation

`mew` is distributed as a CPython 3.11+ package with a small C++ extension
(Google Benchmark via nanobind). On supported platforms a pre-built wheel is
installed; otherwise the C++ extension is compiled from source — see
[](../development/building.md) for the toolchain requirements.

## Using `uv`

With a `[tool.uv.sources]` entry in your `pyproject.toml`:

```toml
[tool.uv.sources]
mew = { git = "https://github.com/nicholasjng/mew" }
```

Simply run:

```console
$ uv add mew
```

## Using `pip`

```console
$ pip install git+https://github.com/nicholasjng/mew.git
```

A PyPI release is planned for the future.

## Optional extras

The following extras are available for extra CLI features:

| Extra      | Pulls in                  | Enables                                                  |
| ---------- | ------------------------- | -------------------------------------------------------- |
| `cpu`      | `pyinstrument`            | `mew run --profile-cpu`, `--cpu-output report.html`      |
| `memory`   | `memray` (non-Windows)    | `mew run --profile-memory`, `--flamegraph alloc.html`    |
| `dev`      | `pytest`, `ruff`, `pyarrow`, `duckdb`, build deps | Local development and Parquet output |

```console
$ uv add 'mew[cpu,memory]'
```

## Verifying

```console
$ mew --version
mew 0.1.0 (Google Benchmark v1.9.0@abcdef12)
```

The trailing version identifies the last stable version, and which Google Benchmark commit the C++ extension was built against.

# Building from source

`mew` is built with CMake using [scikit-build-core](https://scikit-build-core.readthedocs.io/), and the C++ extension uses [nanobind](https://nanobind.readthedocs.io/).

## Prerequisites

- Python 3.11+
- CMake ≥ 3.21
- A C++17 compiler (Clang, GCC, MSVC).
- [`uv`](https://docs.astral.sh/uv/) — recommended for environments and locking.
- [`prek`](https://pre-commit.com/) — pre-commit runner used in CI.

## One-time setup

```console
$ uv sync --all-extras
$ uvx prek install
```

`uv sync` builds the C++ extension into the project's `.venv` via scikit-build-core.
The `[tool.uv]` config in `pyproject.toml` opts the project out of build isolation, so nanobind include paths in`compile_commands.json` survive wheel builds.
This is useful for C++ language servers like `clangd`.

## Rebuilding after a C++ change

```console
$ uv sync --reinstall-package=mew  # editable install picks up the rebuilt .so
```

Alternatively, rebuild using CMake directly:

```console
$ cmake --build build
```

## Test, lint, type-check

```console
$ uv run pytest tests/ -q
$ uvx prek run --all-files
$ uvx ty check src/mew/ tests/
```

These steps run in CI (`.github/workflows/ci.yml`) across Linux, macOS, and Windows on Python 3.11–3.14.

## Building the documentation locally

```console
$ uv pip install -e '.[docs]'
$ uv run sphinx-build -W --keep-going -b html docs docs/_build/html
$ open docs/_build/html/index.html
```

The `-W` flag mirrors readthedocs' `fail_on_warning: true` switch.

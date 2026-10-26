# Building from source

`mew` is built with CMake using [scikit-build-core](https://scikit-build-core.readthedocs.io/), and the C++ extension uses [nanobind](https://nanobind.readthedocs.io/).

## Prerequisites

- Python 3.11+
- CMake ≥ 3.21
- A C++17 compiler (Clang, GCC, MSVC).
- [`uv`](https://docs.astral.sh/uv/): recommended for environments and locking.
- [`prek`](https://github.com/j178/prek): pre-commit runner used in CI.

## One-time setup

```console
$ uv sync --all-groups --all-extras
$ uvx prek install
```

`uv sync` builds the C++ extension into the project's `.venv` via scikit-build-core.
Package builds use standard build isolation.

Configure the stable developer tree for C++ language servers:

```console
$ uv run --no-sync python scripts/configure-clangd.py
```

This copies `build/clangd/compile_commands.json` to the repository root.

## Rebuilding after a C++ change

```console
$ uv sync --reinstall-package=mew-bench  # editable install picks up the rebuilt .so
```

To check that the C++ compiles without reinstalling, build the stable developer tree directly.
The editable install does not load this tree's extension, so run the `uv sync` above before testing:

```console
$ cmake --build build/clangd
```

The normal build generates `_core.pyi` in the build tree, while installs use the checked-in, formatted copy.
After changing the native API, refresh that copy explicitly:

```console
$ cmake --build build/clangd --target update_mew_core_stub
```

CI verifies that the generated and checked-in files match.
Run the same check locally with:

```console
$ cmake --build build/clangd --target check_mew_core_stub
```

## Rebuilding after a dependency bump

After bumping a native dependency (for example, nanobind), clean `build/`: Ninja can retain objects compiled against older headers.

```console
$ rm -rf build/
$ uv sync --reinstall-package=nanobind --reinstall-package=mew-bench
```

## Building without network access

A build fetches Google Benchmark from GitHub at the commit pinned in `CMakeLists.txt` and applies the patches in `patches/`.
To build offline, as for a distribution's package, point CMake at a copy of that commit's source with the patches applied in order:

```console
$ git -C /path/to/benchmark apply "$PWD"/patches/*.patch
$ CMAKE_ARGS="-DFETCHCONTENT_SOURCE_DIR_GOOGLEBENCHMARK=/path/to/benchmark" pip install .
```

CMake uses that tree as it is and doesn't patch it, so the build checks that the patches are there and stops if they're not.
mew depends on them.

## Test, lint, type-check

```console
$ uv run pytest tests/ -q
$ uvx prek run --all-files
```

These steps run in CI (`.github/workflows/ci.yml`) on Python 3.11–3.15 on Linux, and on 3.11 and 3.15 on macOS and Windows.
The ASAN and UBSAN [sanitizer builds](sanitizers.md) run in `.github/workflows/asan.yml` and `ubsan.yml` when native code or its build changes, and weekly.
The TSAN build in `tsan.yml` runs weekly.

## Free-threaded (3.14t+) build

Free-threaded Python builds a version-specific extension.
Build a separate free-threaded editable install in `.venv-ft` so it doesn't clobber the default `.venv`:

```console
$ UV_PROJECT_ENVIRONMENT=.venv-ft uv sync --python 3.14t --all-extras --group test
```

The optional `duckdb` dependency group is for GIL builds, so omit it when testing free-threaded Python.

Confirm the GIL stays disabled after importing the extension:

```console
$ .venv-ft/bin/python -c "import sys, mew._core; assert not sys._is_gil_enabled()"
```

Threaded benchmarks (the `threads` option) only run here.
On a GIL interpreter, mew skips them with a warning to avoid deadlocking on Google Benchmark's start barrier.

```console
$ UV_PROJECT_ENVIRONMENT=.venv-ft uv run --no-sync pytest
```

## Building the documentation locally

```console
$ uv run --group docs sphinx-build -W --keep-going -b html docs docs/_build/html
$ open docs/_build/html/index.html
```

The `-W` flag mirrors readthedocs' `fail_on_warning: true` switch.

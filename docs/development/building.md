# Building from source

`mew` is built with CMake using [scikit-build-core](https://scikit-build-core.readthedocs.io/), and the C++ extension uses [nanobind](https://nanobind.readthedocs.io/).

## Prerequisites

- Python 3.11+
- CMake ≥ 3.21
- A C++17 compiler (Clang, GCC, MSVC).
- [`uv`](https://docs.astral.sh/uv/): recommended for environments and locking.
- [`prek`](https://pre-commit.com/): pre-commit runner used in CI.

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

This copies `build/clangd/compile_commands.json` to the repository root. Its
nanobind paths refer to the project environment rather than a temporary build
environment.

## Rebuilding after a C++ change

```console
$ uv sync --reinstall-package=mew-bench  # editable install picks up the rebuilt .so
```

Alternatively, rebuild the stable developer tree directly:

```console
$ cmake --build build/clangd
```

The normal build generates `_core.pyi` in the build tree, while installs use the
checked-in, formatted copy. After changing the native API, refresh that copy explicitly:

```console
$ cmake --build build/clangd --target update_mew_core_stub
```

CI verifies that the generated and checked-in files match. Run the same check locally with:

```console
$ cmake --build build/clangd --target check_mew_core_stub
```

## Rebuilding after a dependency bump

After bumping a native dependency (for example, nanobind), clean `build/`:
Ninja can retain objects compiled against older headers.

```console
$ rm -rf build/
$ uv sync --reinstall-package=nanobind --reinstall-package=mew-bench
```

## Test, lint, type-check

```console
$ uv run pytest tests/ -q
$ uvx prek run --all-files
```

These steps run in CI (`.github/workflows/ci.yml`) across Linux, macOS, and Windows on Python 3.11–3.14.

## AddressSanitizer

Build a separate ASAN wheel (lands in `build/asan/`, leaving the Release wheel
alone; a plain `uv sync` afterwards swaps the editable install back to Release):

```console
$ MEW_ASAN=1 uv sync --all-groups --reinstall-package=mew-bench
```

`uv run pytest` alone does *not* preload the ASAN runtime, so the test process
aborts at the first import of an ASAN-built extension. Preload it and `libstdc++`
explicitly, invoking the venv's python directly rather than through `uv run`:

```bash
LD_PRELOAD="$(gcc -print-file-name=libasan.so) $(gcc -print-file-name=libstdc++.so)" \
    ASAN_OPTIONS="detect_leaks=0:halt_on_error=1" .venv/bin/python -m pytest --capture no

# for macOS, there's a wrapper script to work around SIP.
scripts/asan-pytest.sh
```

## Free-threaded (3.14t+) build

Free-threaded Python builds a version-specific extension.
Use `.venv-ft` to keep it separate from the normal environment.

Build a separate free-threaded editable install in `.venv-ft` so it doesn't
clobber the default `.venv`:

```console
# duckdb does not ship wheels with free-threading support yet.
$ UV_PROJECT_ENVIRONMENT=.venv-ft uv sync --python 3.14t --all-extras --all-groups --no-install-package duckdb
```

Confirm the GIL stays disabled after importing the extension:

```console
$ .venv-ft/bin/python -c "import sys, mew._core; assert not sys._is_gil_enabled()"
```

Threaded benchmarks (`threads`, `thread_range`, or `dense_thread_range`) only run here; on a GIL
interpreter, mew skips them with a warning to avoid deadlocking on Google Benchmark's start barrier.

```console
$ UV_PROJECT_ENVIRONMENT=.venv-ft uv run pytest
```

## ThreadSanitizer

Once threaded-mode benchmarks are in play, a TSAN build smokes out data races
the ASAN build can't see. It mirrors the ASAN flow (lands in `build/tsan/`, and
is mutually exclusive with ASAN):

```console
$ MEW_TSAN=1 uv sync --all-extras --all-groups --no-install-package duckdb --reinstall-package=mew-bench
```

Preload the TSAN runtime when running, the same way ASAN needs preloading
(`gcc -print-file-name=libtsan.so` on Linux);
on macOS, there's a wrapper script that handles the SIP workaround:

```bash
LD_PRELOAD="$(gcc -print-file-name=libtsan.so) $(gcc -print-file-name=libstdc++.so)" \
    TSAN_OPTIONS="halt_on_error=1:detect_deadlocks=0" .venv/bin/python -m pytest --capture no

# macOS
scripts/tsan-pytest.sh
```

## Building the documentation locally

```console
$ uv pip install -e '.[docs]'
$ uv run sphinx-build -W --keep-going -b html docs docs/_build/html
$ open docs/_build/html/index.html
```

The `-W` flag mirrors readthedocs' `fail_on_warning: true` switch.

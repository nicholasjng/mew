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
$ uv sync --all-extras
$ uvx prek install
```

`uv sync` builds the C++ extension into the project's `.venv` via scikit-build-core.
The `[tool.uv]` config in `pyproject.toml` opts out of build isolation, so nanobind include paths in `compile_commands.json` survive wheel builds, useful for C++ language servers like `clangd`.

## Rebuilding after a C++ change

```console
$ uv sync --reinstall-package=mew  # editable install picks up the rebuilt .so
```

Alternatively, rebuild the configured tree directly. The build dir is
wheel-tag-specific (`build/{wheel_tag}`), so adjust the path to yours:

```console
$ cmake --build build/cp312-abi3-macosx_26_0_arm64
```

## Rebuilding after a dependency bump

The `build/{wheel_tag}` tree persists across rebuilds for fast incremental
compiles. After bumping a native dependency (e.g. nanobind), object files in it
may have been compiled against the old headers, and because ninja decides what
to recompile from file timestamps, a freshly installed header whose mtime
doesn't exceed the cached object can be skipped, which can cause linker errors
and ABI mismatches. Force a clean rebuild with:

```console
$ rm -rf build/
$ uv sync --reinstall-package=nanobind --reinstall-package=mew
```

## Test, lint, type-check

```console
$ uv run pytest tests/ -q
$ uvx prek run --all-files
$ uv run --all-extras --group test ty check
```

These steps run in CI (`.github/workflows/ci.yml`) across Linux, macOS, and Windows on Python 3.11–3.14.

## AddressSanitizer

Build a separate ASAN wheel (lands in `build/asan/`, leaving the Release wheel
alone; a plain `uv sync` afterwards swaps the editable install back to Release):

```console
$ MEW_ASAN=1 uv sync --all-groups --reinstall-package=mew
```

`uv run pytest` alone does *not* preload the ASAN runtime, so the test process
aborts at the first import of an ASAN-built extension. Preload it explicitly:

```bash
# Linux: preload the ASAN runtime *and* libc++. libc++ must be preloaded too,
# otherwise its container-overflow annotations don't line up with the
# instrumented build and ASAN reports false positives.
MEW_ASAN=1 LD_PRELOAD="$(clang -print-file-name=libclang_rt.asan.so) $(clang -print-file-name=libc++.so)" \
    ASAN_OPTIONS="detect_leaks=0:halt_on_error=1" uv run --all-groups pytest --capture no

# for macOS, there's a wrapper script to work around SIP.
scripts/asan-pytest.sh
```

## Free-threaded (3.14t+) build

mew's extension is built with nanobind's `FREE_THREADED` flag. nanobind keeps
the stable-ABI (`cp312`) wheel on a stock interpreter and switches to a
version-specific free-threaded wheel (`Py_MOD_GIL_NOT_USED`) on a free-threaded
one; the two ABIs are mutually exclusive on 3.13/3.14, so the
`if.abi-flags = "t"` override in `pyproject.toml` drops `wheel.py-api` there.

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

Threaded benchmarks (`threads` / `thread_range`) only run here; on a GIL
interpreter, mew skips them with a warning to avoid deadlocking on Google Benchmark's start barrier.

```console
$ UV_PROJECT_ENVIRONMENT=.venv-ft uv run pytest
```

## ThreadSanitizer

Once threaded-mode benchmarks are in play, a TSAN build smokes out data races
the ASAN build can't see. It mirrors the ASAN flow (lands in `build/tsan/`, and
is mutually exclusive with ASAN):

```console
$ MEW_TSAN=1 uv sync --all-extras --all-groups --no-install-package duckdb --reinstall-package=mew
```

Preload the TSAN runtime when running, the same way ASAN needs preloading
(`clang -print-file-name=libclang_rt.tsan.so` on Linux); on macOS there's a
wrapper script that handles the SIP workaround:

```console
$ scripts/tsan-pytest.sh
```

Race reports on the free-threaded path are most useful run against a benchmark
that uses `threads` with a deliberately-racy body, so point `VIRTUAL_ENV` at a
free-threaded, TSAN-built `.venv-ft` (see the script header).

## Building the documentation locally

```console
$ uv pip install -e '.[docs]'
$ uv run sphinx-build -W --keep-going -b html docs docs/_build/html
$ open docs/_build/html/index.html
```

The `-W` flag mirrors readthedocs' `fail_on_warning: true` switch.

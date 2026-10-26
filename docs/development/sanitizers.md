# Sanitizer builds

The sanitizer builds instrument the bindings and Google Benchmark in separate Debug builds, each under its own `build/` directory.
Use one sanitizer environment switch at a time, and reinstall without it to return to the Release build.

## AddressSanitizer

Build a separate ASAN wheel (it lands in `build/asan/`, leaving the Release wheel alone).
The same command without `MEW_ASAN` swaps the editable install back to Release:

```console
$ MEW_ASAN=1 uv sync --group test --group duckdb --reinstall-package=mew-bench
```

`uv run pytest` alone does *not* preload the ASAN runtime, so you must preload it and `libstdc++` explicitly, invoking the venv's python directly rather than through `uv run`:

```bash
PYTHONMALLOC=malloc \
    LD_PRELOAD="$(gcc -print-file-name=libasan.so) $(gcc -print-file-name=libstdc++.so)" \
    ASAN_OPTIONS="detect_leaks=0:halt_on_error=1:detect_stack_use_after_return=1:strict_string_checks=1:log_path=$PWD/build/asan/report" \
    .venv/bin/python -m pytest --capture no

# for macOS, there's a wrapper script to work around SIP.
scripts/sanitizer-pytest.sh asan
```

ASAN enables stack-use-after-return and strict C-string checks.
On GIL builds, `PYTHONMALLOC=malloc` exposes individual Python allocations to ASAN.
The macOS wrapper selects this allocator unless the caller already set `PYTHONMALLOC`.
It keeps the required mimalloc allocator on free-threaded Python.
Linux reports are saved as `build/asan/report.<pid>`.
The macOS wrapper prints its reports when pytest exits.

With Memray installed, macOS ASAN runs can stall during Memray's capture-file teardown, including with the original ASAN settings.
A focused macOS TSAN run of those integrations also terminated without a sanitizer diagnostic.
Run those optional integration tests without sanitizers.
To check the rest of the suite locally:

```console
$ scripts/sanitizer-pytest.sh asan -k 'not memray and not flamegraph'
```

## UndefinedBehaviorSanitizer

UBSAN checks undefined behavior such as signed integer overflow, invalid shifts, and misaligned pointer accesses.
Like ASAN, it instruments the bindings and Google Benchmark in a separate Debug build, in `build/ubsan/`:

```console
$ MEW_UBSAN=1 uv sync --group test --group duckdb --reinstall-package=mew-bench
```

The UBSAN build exits on its first finding.

On Linux, preserve diagnostics outside benchmark stderr capture:

```bash
LD_PRELOAD="$(gcc -print-file-name=libubsan.so) $(gcc -print-file-name=libstdc++.so)" \
    UBSAN_OPTIONS="halt_on_error=1:print_stacktrace=1:log_path=$PWD/build/ubsan/report" \
    .venv/bin/python -m pytest --capture no
```

Reports are saved as `build/ubsan/report.<pid>`.
On macOS, the wrapper preloads the runtime and prints reports after pytest exits:

```console
$ scripts/sanitizer-pytest.sh ubsan
```

`.github/workflows/ubsan.yml` runs the Linux check on native/build changes and weekly, and prints reports even when pytest fails.

## ThreadSanitizer

Once threaded-mode benchmarks are in play, a TSAN build smokes out data races the ASAN build can't see.
It mirrors the ASAN flow (lands in `build/tsan/`, and is mutually exclusive with ASAN):

```console
$ MEW_TSAN=1 uv sync --all-extras --group test --reinstall-package=mew-bench
```

Preload the TSAN runtime when running, the same way ASAN needs preloading (`gcc -print-file-name=libtsan.so` on Linux).
On macOS, the wrapper script handles the SIP workaround:

```bash
LD_PRELOAD="$(gcc -print-file-name=libtsan.so) $(gcc -print-file-name=libstdc++.so)" \
    TSAN_OPTIONS="halt_on_error=1:detect_deadlocks=0:print_full_thread_history=1:log_path=$PWD/build/tsan/report" \
    .venv/bin/python -m pytest --capture no

# macOS
scripts/sanitizer-pytest.sh tsan
```

The wrapper and Linux job include thread-creation ancestry in TSAN reports.
The Linux job builds CPython itself with TSAN.
Instrumenting only mew cannot give complete coverage of interactions with the interpreter.

## Additional sanitizer diagnostics

Keep the normal settings for routine runs.
For a focused reproduction, append these options to the existing `ASAN_OPTIONS` or `TSAN_OPTIONS`:

| Option | Use |
| --- | --- |
| ASAN `fast_unwind_on_malloc=0` | Use the slower unwinder when allocation/free stacks are incomplete. |
| ASAN `detect_leaks=1` | On a runtime with LeakSanitizer support, check native lifetime tests for leaks at exit and establish a baseline with minimal imports first. |
| TSAN `history_size=1` | Retain more access history when a report cannot restore an earlier stack, increasing gradually at a memory cost. |

Apple's ASAN runtime rejects `detect_leaks=1`.
Use the Linux build for leak checks.

Check the installed runtime's `help=1` output before tuning history size or using newer options such as TSAN `enable_adaptive_delay=1`.
Adaptive delay perturbs scheduling to expose races and is suitable for a separate stress run.
Preserve `halt_on_error=1`, report paths, and the Linux job's CPython suppression file.
In the macOS wrapper, setting an options variable replaces its tool defaults, so include the defaults when supplying extra options.

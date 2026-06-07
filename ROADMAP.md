# Roadmap

Legend: ✅ shipped · 🟡 partially shipped · ⬜ not started.

## Free-threading (CPython 3.13t+)

- ⬜ **Free-thread-ready module import.** Add `FREE_THREADED` to
  `nanobind_add_module(_core …)` in `CMakeLists.txt` so the extension emits
  `Py_MOD_GIL_NOT_USED` and `import mew._core` doesn't re-enable the GIL on a
  3.13t interpreter. Zero behavioural change for current users on 3.12 / 3.13
  default — the flag is a no-op when the GIL is enabled. Two follow-on bits of
  work: (1) verify nanobind's `STABLE_ABI` (we're on `cp312`) is co-compatible
  with `FREE_THREADED`; the free-threaded ABI tag is distinct, so we may need
  a separate FT wheel or drop STABLE_ABI for the FT variant; (2) add a CI job
  that imports `mew._core` on a 3.13t interpreter and asserts
  `sys._is_gil_enabled() is False`, so any future change that accidentally
  re-enables the GIL is caught at PR time. This is the door-opener for the
  rest of the section; nothing downstream of it changes mew's C++ behaviour.

- ⬜ **Expose Google Benchmark's threaded mode.** Bind
  `BenchmarkHandle.threads(n)`, `.thread_range(min, max)`, and
  `.thread_per_cpu()` — one-liner member-pointer bindings in the same shape as
  `.min_time(...)` etc. (`src/_mew/registry.cpp`). Add `threads: int | None`
  and `thread_range: tuple[int, int] | None` to `BenchmarkOptions`
  (`src/mew/_typing.py`); plumb through `runner._apply_options`. Each thread
  gets its own `State` and timer (see audit below), so the C++ side is
  thread-clean; the catch worth documenting prominently is the GIL: on a GIL
  interpreter, GBM still spawns N OS threads, but mew's `register_benchmark`
  trampoline acquires the GIL on every call, so the N threads serialise —
  `state.threads` / `state.thread_index` are accurate, but wall-clock timing
  reflects 1-thread-at-a-time-in-Python rather than real parallelism. The
  docstring on `.threads()` and the docs page on parametrization both need a
  "threaded mode is only meaningful on free-threaded interpreters" callout.
  Shipping these bindings without the warning is the failure mode to avoid.

- ⬜ **Audit the actual FT hazard sites.** Each GBM thread constructs its
  own `State` with its own per-thread `ThreadTimer`
  (`benchmark_runner.cc:144-171`), and per-thread results are merged under
  `manager->GetBenchmarkMutex()` on thread completion. So `pause()` is
  per-thread by construction, and `set_counter` / `set_label` etc. write to a
  per-thread `State.counters` map that GBM `Increment`s into the merged
  result — the "call from `thread_index == 0` only" GBM convention is
  reporting hygiene (one label, one `set_items_processed` per benchmark) not
  memory safety. The sites worth checking on FT builds instead:
  - **nanobind's instance cache** when `nb::cast(&s, reference)` runs
    concurrently in N trampolines. Different threads cast different `State*`,
    so they don't collide on the same entry, but the cache itself needs
    FT-safe lookup. nanobind handles this internally via `nb::ft_mutex` once
    the module is built with `FREE_THREADED`; verify clean under TSAN.
  - **The `shared_ptr<nb::callable>` holder** captured in
    `register_benchmark`'s lambda (`src/_mew/registry.cpp`). Control block
    is atomic; the wrapped Python callable runs under whatever FT semantics
    Python applies. No code change expected; verify.
  - **The user's benchmark body.** N concurrent invocations of one Python
    callable. Thread-safety is the user's responsibility — the docs should
    show `state.thread_index` for work-partitioning alongside the threaded-
    mode section.

- ⬜ **CI matrix entry for 3.13t.** Add a job that builds the extension on a
  3.13t interpreter, imports `mew._core`, asserts GIL is off, and runs the
  Python test suite. The big unknown is which of mew's dev / runtime deps
  publish 3.13t wheels yet (pyarrow, duckdb, polars, rich, pyinstrument all
  matter for the reporter / profile paths); the CI matrix should start with
  the minimum subset needed to import mew and run benchmark execution, and
  expand as upstream wheels appear. Without a CI job here, any of the items
  above can silently regress on the FT path between releases.

- ⬜ **ThreadSanitizer pass.** The ASAN config from `DEVELOPMENT.md` catches
  use-after-free but not data races. Once the threaded-mode bindings land and
  the FT CI job is green, a TSAN build run against a benchmark file that uses
  `.threads()` with a deliberately-racy callback would smoke out anything the
  source audit missed — particularly around `pause()` and the shared
  `state.counters` map on the C++ side.

## Reporter / output

- ⬜ **Streaming `report_runs` rather than batched.** Today
  `PyReporter::ReportRuns` (`src/_mew/reporter.cpp`) gets a `std::vector<Run>`
  and copies every entry into a Python list before invoking the callback.
  For long parametrized families with many repetitions this peaks memory at
  `O(runs)` for no benefit; the reporter doesn't aggregate, it just writes.
  Switching the callback contract to "called once per `Run`" would keep peak
  memory bounded and let progress reporters render rows as they complete. The
  Python-side `Fanout` wrapper would need the same per-run callback shape.

- ⬜ **Per-case discovery in `mew list`.** The indexing-based parametrize
  rewrite collapsed each family to a single row in `mew list` (one
  `Entry`, one `case_labels` list). Power users who want to filter to a
  single case currently fall back to Google Benchmark's `--benchmark_filter`
  regex at run time. A `mew list --show-cases` flag (or expanding by default
  when `-k` matches a family) would expose the case axis to discovery
  without forcing the registry back into N entries per family.

## Comparison story

- ⬜ **Session-addressable comparisons inside a single file.** Today
  `mew compare` only takes the cross-file shape (`mew compare a.parquet
  b.parquet`); within a single file, `compare._load` collapses sessions to
  the latest by `(date, host_name)` and warns about discards. That breaks
  two real workflows: comparing two runs that share a timestamp (same
  second, same host — keys collide, one silently wins the latest tie-break),
  and pointing at a run by a stable name (there is no such name — only date
  + host are persisted today).

  The fix is one change with a write-time and a read-time half:
  - **Write-time identity.** Generate a short ULID (or UUIDv7) at `mew run`
    startup and write a `session_id` column on every row. Collisions are
    gone by construction; this is the canonical key for any session
    selector. Optional `mew run --session-tag NAME` writes a `session_tag`
    column — auto-derived from `git describe --always --dirty` when unset
    and the cwd is a repo (opt out via `--no-auto-session-tag` / env),
    since that's the overwhelmingly common case for benchmark archives.
    Distinct from the existing `mew run --tag` / `-t` which filters *which*
    benchmarks to run by their Python-side `tags=` decorator argument —
    `--session-tag` labels the *whole run's output*, not the selection.
  - **Read-time selector grammar.** Refactor `compare._load` to return a
    list of `(session_key, dict[name, Sample])` instead of collapsing.
    Positional selectors then support: `path` (latest session in the file,
    today's default), `path@<tag>` (exact `session_tag` match), `path@<id>`
    (git-style short prefix match on `session_id`, ≥4 chars, error on
    ambiguous), and `path@latest` / `@earliest` / `@N` as ordinal fallbacks
    for older files without identifiers. `--baseline` becomes unnecessary —
    positional order already encodes that.
  - **Same-timestamp flow** then looks like
    `mew run --session-tag before -o results.parquet`,
    `mew run --session-tag after -o results.parquet`,
    `mew compare results.parquet@before results.parquet@after`.
    Tag-free workflow: `mew compare results.parquet@01HXY...
    results.parquet@01HXZ...` using session_id prefixes.
  - **Migration.** Files written before this lands have no `session_id` /
    `session_tag` columns; `_load` falls back to synthesising a key from
    `(date, host_name)` so pre-existing `mew compare a.parquet b.parquet`
    invocations keep working unchanged.

  **Usage guidance — files vs sessions.** Sessions are deliberately *not*
  the primary surface for CI flows. Files are. A PR-vs-master pipeline
  should write one Parquet per CI run (`bench-master-<sha>.parquet`,
  `bench-pr-1234.parquet`) and compare with `mew compare baseline.parquet
  bench-pr-1234.parquet` — the filesystem is the artifact boundary, the
  file name is the column label, and the existing two-file code path needs
  no changes. `--session-tag` and the `path@<tag>` selector are for the
  case where two runs belong in one file *by intent*: local before/after
  experiments, sprint lab-notebook archives, side-by-side configurations
  evaluated in one go.

  **Out of scope — query-style selection.** mew compare reads a
  well-shaped result file with one or two sessions of interest; it is
  *not* a query engine over a growing benchmark archive. Workflows like
  "compare this PR against the most recent master commit's benchmarks in
  bench-history.parquet" need tag-pattern selectors, rolling-tag
  semantics, time-window joins, etc. — query work that belongs upstream
  of `mew compare`. The expected shape is a SQL pre-processing step (in
  ducky / DuckDB / polars) that materialises a fresh Parquet with exactly
  the two sessions you want, which mew then compares. `session_id` and
  `session_tag` are persisted partly so they're stable join keys for that
  upstream SQL.

## Public API surface

- ⬜ **Async runner.** `mew.run` is sync; `RunSpecifiedBenchmarks` runs to
  completion on the calling thread. An `async def arun(...)` that offloads
  the C++ call via `asyncio.to_thread` (the GIL is already released inside
  `run_benchmarks` via the existing `gil_scoped_release`) would let
  notebooks / async test runners drive mew without spawning their own
  worker thread. Cancellation is the open question — GBM has no public
  "stop after current iteration" hook; the natural escape valve is
  signalling the benchmark to set `skip_with_message("cancelled")` from
  inside its loop, which only helps when the benchmark cooperates.

- ⬜ **Custom statistics.** GBM supports `.ComputeStatistics(name, fn)` for
  user-defined aggregate functions over repetitions (median, p95, …). The
  binding shape mirrors `register_benchmark`: a Python callable that takes
  `list[float]` and returns a float. Useful for projects whose
  "did-we-regress" gate is a non-default statistic.

## Build / packaging

- ⬜ **Vendor a Google Benchmark release tag.** Today we pin to a `main`
  commit (`MEW_BENCHMARK_COMMIT` in `CMakeLists.txt`) because the
  pause-timing RAII helper we use isn't in any tagged release yet. Once a GBM
  release with that helper ships, bump the pin to the release SHA — same
  reproducibility, but easier to communicate ("we're on v1.10.0") than a raw
  commit. The cache variable stays a `STRING` override, so testing against
  any post-bump GBM commit remains a `-DMEW_BENCHMARK_COMMIT=<sha>` away.

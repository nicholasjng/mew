# Roadmap

Legend: ✅ shipped · 🟡 partially shipped · ⬜ not started.

## Free-threading (CPython 3.13t+)

- ✅ **Free-thread-ready module import.** Shipped:
  `nanobind_add_module(_core STABLE_ABI FREE_THREADED …)` in `CMakeLists.txt`.
  nanobind keeps exactly the flag that fits the interpreter — `cp312` abi3 on a
  stock build (FREE_THREADED dropped), and a version-specific free-threaded wheel
  emitting `Py_MOD_GIL_NOT_USED` on a 3.13t/3.14t build (STABLE_ABI dropped,
  since the limited API has no free-threaded variant). The
  `if.abi-flags = "t"` scikit-build-core override clears `wheel.py-api` so the
  FT build isn't tagged abi3. Verified end-to-end: `import mew._core` leaves
  `sys._is_gil_enabled()` False on 3.13t. The STABLE_ABI/FREE_THREADED
  co-compatibility question is resolved — pass both, nanobind picks.

- ✅ **Expose Google Benchmark's threaded mode.** Shipped:
  `BenchmarkHandle.threads(n)`, `.thread_range(min, max)`, `.thread_per_cpu()`
  (`src/_mew/registry.cpp`); `threads: int` / `thread_range: tuple[int, int]` on
  `BenchmarkOptions`, plumbed through `runner._apply_options` and `@product`'s
  explicit kwargs. **Correction to the original note:** threaded mode does *not*
  "serialise" on a GIL interpreter — it **deadlocks**. The trampoline holds the
  GIL across Google Benchmark's per-thread start barrier (`StartStopBarrier`),
  so thread 0 waits for siblings that can never acquire the GIL to reach the
  barrier. `mew.run` therefore detects threaded entries on a GIL build and, by
  default, **warns and skips** them (emitting a `skipped` row per benchmark) so a
  mixed suite still runs on stock CPython — `mew run --strict` / `run(strict=True)`
  turns the skip into a hard `RuntimeError` for CI. Threaded mode is
  free-threaded-only, documented in `docs/guide/state-and-timing.md`.

- ✅ **Audit the actual FT hazard sites.** Done — and it surfaced a real
  deadlock the source audit alone would have missed. The C++ side is
  thread-clean as predicted (per-thread `State`/`ThreadTimer`, merge under
  `GetBenchmarkMutex`, nanobind's `nb::ft_mutex`-guarded instance cache, atomic
  `shared_ptr` control block — all verified on the FT build). The hazard was in
  CPython itself: Google Benchmark spawns N *raw* worker threads that each call
  `PyGILState_Ensure` at once, and the first one to cross the single→multi-thread
  boundary triggers `_PyGC_ImmortalizeDeferredObjects`, a **stop-the-world** pass
  that deadlocks against siblings blocked mid-`_PyThreadState_Attach` (confirmed
  with a native `sample` trace). Fix: `runner._warmup_free_threading` drives that
  transition on a clean main thread (spawn+join one `threading.Thread`) before
  GB launches its workers, making the STW a no-op by attach time. Only invoked
  on the free-threaded threaded path. The user-body thread-safety note is in the
  docs. Still open: the TSAN pass below (source audit + a runtime sanitizer are
  complementary).

- ✅ **CI matrix entry for 3.13t.** Shipped: the `free-threaded · py3.13t` job
  in `ci.yml` builds the extension on 3.13t, asserts `sys._is_gil_enabled()` is
  False after `import mew._core`, and runs a dependency-light test subset
  (including a real threaded-benchmark run that would hang on a deadlock
  regression). Confirmed empirically that the result-path deps (pyarrow, duckdb,
  memray, pyinstrument) still build from source on 3.13t — no FT wheels yet — so
  the subset is intentionally minimal per the original note; grow it as upstream
  wheels appear.

- 🟡 **ThreadSanitizer pass.** Infra shipped: a `MEW_TSAN` CMake option +
  scikit-build-core override (`build/tsan/`), mirroring ASAN and mutually
  exclusive with it; documented in `docs/development/building.md`. Not yet *run*
  in CI against a deliberately-racy `.threads()` callback — that's the remaining
  half. The source audit above found the deadlock; TSAN is the backstop for any
  data race it didn't, particularly around `pause()` and the shared
  `state.counters` map.

## Reporter / output

- ⬜ **Streaming `report_runs` rather than batched.** Today
  `PyReporter::ReportRuns` (`src/_mew/reporter.cpp`) gets a `std::vector<Run>`
  and copies every entry into a Python list before invoking the callback.
  For long parametrized families with many repetitions this peaks memory at
  `O(runs)` for no benefit; the reporter doesn't aggregate, it just writes.
  Switching the callback contract to "called once per `Run`" would keep peak
  memory bounded and let progress reporters render rows as they complete. The
  Python-side `Fanout` wrapper would need the same per-run callback shape.

- ✅ **Per-case discovery in `mew list`.** Shipped: `mew list --show-cases`
  expands each family into one row per case (`name[label]`), and `-F` / `--literal`
  lets a displayed `name[label]` be pasted into `-k` to run a single case without
  escaping its brackets. The registry still holds one `Entry` per family
  (expansion is display-only). Original note below. The indexing-based parametrize
  rewrite collapsed each family to a single row in `mew list` (one
  `Entry`, one `case_labels` list). Power users who want to filter to a
  single case currently fall back to Google Benchmark's `--benchmark_filter`
  regex at run time. A `mew list --show-cases` flag (or expanding by default
  when `-k` matches a family) would expose the case axis to discovery
  without forcing the registry back into N entries per family.

- ⬜ **Lifecycle trace reporter (Chrome trace-event JSON).** A reporter that
  emits mew's own execution *structure* as a timeline — warmup vs. measured
  phases, per-repetition spans, `state.pause()` regions, counters over time —
  in the Chrome Trace Event format (`{"traceEvents": [...]}`). That format is
  plain JSON (no protobuf, no SDK/daemon) and loads directly in both the
  Perfetto UI and `chrome://tracing`, so it's a small reporter, not a
  dependency on a tracing stack. **Explicitly not a sampling profiler:** this
  answers "what was the shape of this run — where did warmup end, which
  repetition was the outlier, what did the pause regions cost," which is a
  distinct view from `mew profile`'s "where did the CPU time go." Keeping that
  line bright is the point — Perfetto is a great viewer for *this* (timeline
  data), and a poor fit for hot-loop CPU sampling, where speedscope/pprof win.
  The open design question is how much structure the C++ `Run` stream exposes
  to the reporter today (phase boundaries, per-rep timestamps) versus what
  would need new plumbing through `PyReporter::ReportRuns`.

## Profiling

`mew profile` writes speedscope-readable artifacts today (xctrace `.trace` for
macOS Instruments; py-spy speedscope JSON and `perf script` text on Linux, both
loadable at speedscope.app). speedscope is the zero-install common format and the
default; the items here are opt-in additions, not replacements.

- ⬜ **`--format pprof` for the perf (and py-spy) backend.** pprof's payoff is
  not a prettier flamegraph — speedscope already covers that — it's
  `pprof -http=:8080 -diff_base=baseline.pb.gz head.pb.gz`, a *sample-level diff*
  that complements `mew compare`: compare tells you a benchmark regressed, the
  pprof diff shows where the extra samples went. Two implementation notes decide
  whether it's worth it: (1) pprof does **not** read `perf.data` or `perf script`
  directly — the canonical path is Google's `perf_to_profile`
  (`perf_data_converter`), a Bazel-built C++ binary that is not pip-installable
  and rarely present, so depending on it would undercut the clean install story.
  Prefer generating `profile.proto` ourselves from collapsed stacks (~100 lines,
  no external binary); fall back to `perf_to_profile` only if it happens to be on
  PATH. (2) Neither py-spy nor xctrace emit pprof, so this is perf-mostly and does
  not unify the backends — it's a second, optional format axis, which is why it
  stays opt-in behind a flag rather than becoming a default. Non-blocking today:
  we already keep the raw `perf.data` next to each artifact, so a motivated user
  can run `perf_to_profile` + pprof by hand right now; building it in only buys
  convenience and a wired-up diff workflow.

- ⬜ **Regression-triggered differential profiling.** Wire profiling into the
  compare story: when `mew compare --fail-on-regression` flags a benchmark,
  capture baseline-vs-head profiles for *just that benchmark* and diff them, so
  the gate that says "this regressed" also shows *where the time went*. This is
  the natural consumer of the `--format pprof` item above (`pprof -diff_base`
  for the sample-level diff), and it answers the user's actual next question
  after a red gate. Scope decisions: it needs both revisions' code available
  (a CI two-checkout or two-artifact flow, not a single working tree), so the
  first cut is likely a documented recipe — `mew profile -k <regressed>` on each
  side, then diff — before any built-in `--profile-regressions` flag. Keep it
  opt-in: profiling every regressed benchmark on every red gate is expensive
  (see selective profiling below).

- ⬜ **Native memory profiling.** Today memory is only `mew run --profile-memory`
  (memray, in-process, Python-level allocations). The out-of-process path can
  see C-extension `malloc` that memray misses. On macOS this is *already
  reachable* — the xctrace backend is template-driven, so `mew profile --template
  Allocations` (or `Leaks`) records a native allocations trace today; it only
  needs documenting and a friendlier alias. The Linux parallel is a **heaptrack**
  backend, shaped like the perf/py-spy backends (launch the worker under
  `heaptrack`, emit its data file). That rounds `mew profile` out to memory, not
  just CPU. Open question: whether to surface this as `mew profile --what
  {cpu,memory}` or leave it as raw `--template`/backend selection.

- 🟡 **Selective profiling (`--slowest N` / only-regressed).** Shipped:
  `mew profile --slowest N` profiles only the N slowest benchmarks, ranked by a
  prior result file (`--rank-from`) or a quick in-process timing pass; a family
  is ranked by its slowest case. The only-regressed half (profile just the
  benchmarks a `mew compare` gate flagged) is still open — see the
  regression-triggered profiling item above. Profiling a whole suite is
  expensive, and most of it is uninteresting. A selector that profiles
  only the top-N slowest benchmarks (from a prior timing run or a result file),
  or only those a compare flagged, makes "profile the suite" affordable instead
  of all-or-nothing. This is the cost lever the two items above lean on —
  regression-triggered profiling is only practical if it profiles the few
  benchmarks that moved, not the hundred that didn't.

## Comparison story

- ✅ **Variant orchestration for mutually-incompatible processes.** Shipped:
  `mew run --variant name=path` (one subprocess per variant via
  `mew._variant_worker`, repetitions interleaved A B A B…), per-variant
  `set_context`, profiling flags compose, and `mew compare <file> --by variant`
  (defaults to `--key func`). See `docs/guide/variants.md`. Original sketch
  below. "Same
  logical suite, N processes that cannot share an interpreter" is a recurring
  shape: two engines statically linking the same library (the ducky-vs-duckdb
  case in `notes/cross-engine-comparison.md`), GIL vs free-threaded
  interpreters, Python versions, ASAN vs Release builds. Sketch:

  ```console
  $ mew run --variant ducky=benchmarks/bench_ducky.py \
            --variant duckdb=benchmarks/bench_duckdb.py \
            -o results.jsonl && mew compare results.jsonl --by variant
  ```

  One subprocess per variant; rows tagged with the variant name as a
  context/column dimension, *not* part of the benchmark name (`compare --key
  func` already covers the name-matching half). `compare --by variant` pivots
  a single file into baseline-vs-others instead of requiring N files. The big
  win once the orchestrator owns scheduling: **interleave repetitions across
  variants** (A B A B … instead of all-A then all-B) so thermal/load drift
  decorrelates from the variant axis — with process isolation each repetition
  is its own subprocess invocation anyway, so this falls out naturally. The
  `_subprocess_worker` machinery from `mew profile` is the likely launch
  vehicle. Depends on the session-identity work below for clean row tagging;
  the combined design is sketched in `notes/sessions-and-variants.md`.

- 🟡 **Noise instrumentation in `run` output.** A background compile during
  one run once turned a 4.6 ms benchmark into 8.9 ms and nothing flagged it.
  `compare` now marks high-CV rows with `±N% (!)`; the remaining pieces are
  run-side and statistical:
  - Surface per-benchmark CV in the rich reporter when `repetitions > 1`,
    with the same unreliability marker.
  - Record load average (and macOS thermal pressure) at run start/end into
    the context block; warn on a large delta — that's the "something else was
    running" tripwire.
  - With per-repetition rows available, add a significance marker to
    `compare` (Mann-Whitney U, like Google Benchmark's own
    `tools/compare.py`) so a 5% delta reads as "real" or "noise" rather than
    just a colored number. Pairs with the deterministic instruction-count
    item below: one attacks noise statistically, the other removes it from
    the measurement.

- ⬜ **Deterministic instruction-count metric as a low-noise gate.** Wall-clock
  and even CPU-time are noisy, which is what makes `--fail-on-regression` flaky:
  a 3% threshold trips on scheduler jitter, not real regressions. A
  **callgrind/cachegrind** (valgrind) pass counts retired instructions
  *deterministically* — same code yields the same count run-to-run and
  machine-to-machine — so it gates on actual work done, not timing weather. It's
  the model `iai`/`iai-callgrind` use in Rust. Two properties make it the right
  fit here: it's pure software simulation, so it works in **containers and cloud
  CI** where hardware PMUs are unavailable (unlike `perf stat` counters, which
  need PMU access most cloud runners don't expose); and the count is stable
  enough to gate on a *tight* threshold. Cost is the ~20–50× slowdown, so this is
  a separate measurement mode, not part of the timing run: a `callgrind` backend
  that emits an `instructions` metric per benchmark, which `mew compare -m
  instructions --fail-on-regression` then gates on. The number is also a clean
  cross-machine baseline for archived comparisons, since it doesn't drift with
  the host. Note: this is *measurement*, not profiling — but callgrind also
  produces a call graph (`callgrind_annotate` / KCachegrind / pprof can read it),
  so the same pass doubles as a profile artifact when you want one.

- ✅ **Session-addressable comparisons inside a single file.** Shipped: a
  per-run `session_id` and optional `--session-tag` (auto-derived from
  `git describe` unless `[tool.mew] auto_session_tag = false`) on every row, with
  `mew compare path@<selector>` resolving `@latest` / `@earliest`, `@~N` (N back
  from latest), an exact `session_tag`, or a `session_id` prefix (≥4 chars);
  files without identifiers fall back to a `(date, host)` key. Original sketch
  below. (Implementation
  sketch, including how this layers under variant orchestration:
  `notes/sessions-and-variants.md`.) Today
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

- ⬜ **Custom statistics.** User-defined aggregate functions over repetitions
  (median, p95, gmean, …), mainly for a non-default "did-we-regress" gate.
  GBM's `.ComputeStatistics(name, fn)` takes a *raw C function pointer*, not a
  `std::function`, so a Python callable can't bind to it directly — compute the
  statistic Python-side over the per-repetition values instead (which mew already
  receives). Contract is `Callable[[np.ndarray], float]`: mew applies
  `np.asarray` to the per-rep values (don't pass float lists — scipy's array-API
  shift is trending toward rejecting them), so numpy/scipy reducers drop in.
  numpy becomes a lazy optional dep on this path only. Design: see
  [notes/custom-statistics.md](notes/custom-statistics.md).

## Build / packaging

- ⬜ **Vendor a Google Benchmark release tag.** Today we pin to a `main`
  commit (`MEW_BENCHMARK_COMMIT` in `CMakeLists.txt`) because the
  pause-timing RAII helper we use isn't in any tagged release yet. Once a GBM
  release with that helper ships, bump the pin to the release SHA — same
  reproducibility, but easier to communicate ("we're on v1.10.0") than a raw
  commit. The cache variable stays a `STRING` override, so testing against
  any post-bump GBM commit remains a `-DMEW_BENCHMARK_COMMIT=<sha>` away.

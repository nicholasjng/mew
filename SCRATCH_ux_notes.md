# mew UX notes from ducky-vs-duckdb bench session (2026-06-10)

Scratch list of friction points hit while writing
`ducky/benchmarks/run_vs_duckdb.py`. Order is by severity, not by where the fix
lives.

## 1. `--profile-memory` drops everything on parametrize families

`_ProfileEnriching.report_runs` (`src/mew/_profile.py:198`) looks up profiles by
`r.benchmark_name()` — which for parametrized cases is
`benchmarks/foo.py::bench_x/case:0/min_time:0.200`, not the entry name.
`memory_profiles` dict is keyed by the entry name, so the lookup misses on every
parametrize case and `Peak Mem` / `Total Alloc` columns all show `—`.

Repro: `mew run benchmarks/bench_ducky.py --profile-memory -o out.json`. Every
benchmark in `out.json` has `memory: null` despite memray running.

Fix idea: strip `/case:*/min_time:*/...` suffixes off `r.benchmark_name()` before
the lookup, or key `memory_profiles` by the full benchmark_name memray actually
ran under.

## 2. `mew.memory.profile()` only profiles case 0 of a family

`_MockState.range(pos)` returns `0` unconditionally
(`src/mew/_profile.py:88`). For a parametrized family the trampoline dispatches
via `_cases[state.range(0)]`, so only the first case ever runs under memray.
Combined with #1 this means parametrize + memory profiling is effectively
unsupported.

I worked around it by writing my own per-case subprocess driver
(`benchmarks/_memray_one.py`).

Fix idea: expand parametrize families before calling `_profile_mem`, or pass an
index into `_MockState` so a caller can iterate cases manually.

## 3. `mew.memory.profile()` reads every allocation record

`_collect_stats` in `src/mew/memory.py:69` does
`list(reader.get_allocation_records())`. For a 100k-row executemany this is
~26M records; the reader scan ran for >5 min before I killed it (RSS hit 3 GB).

`reader.metadata.total_allocations` gives the count instantly. For total_bytes
you need the records, but most users probably want peak + count, not the sum of
all live allocations. Could split into `MemoryProfile.from_metadata()` (fast)
and `from_records()` (full).

## 4. RichReporter truncates names; case label is hidden

Long entry names like
`benchmarks/bench_vs_duckdb.py::bench_select_fetchall` get rendered as
`benchmarks/bench_vs_duckdb.py:…` in a normal-width terminal. The parametrize
`label` is *known* (it's in the Entry / Run) but isn't shown — so you can't tell
which case a row corresponds to. Made it impossible to read the runtime
results without dumping JSON.

Fix idea: add a `Label` column (visible when any entry has `report_label`);
overflow the name column to ellipsis the *left* side so the meaningful function
suffix survives.

## 5. JSONReporter buffers until `finalize()`

`JSONReporter.finalize()` (`src/mew/reporter.py:120`) builds the whole document
in memory and writes once. For a multi-minute suite there's no partial-results
file on disk, and `mew run ... | tail` is useless because everything is buffered
until exit. I worked around it by piping through `tee` and reading inside the
buffer.

Fix idea: a streaming JSONL reporter (one row per line, flushed) that's the
default when `-o file.jsonl`. Or just `flush=True` after each `report_runs`.

## 6. `--pattern` semantics

`--pattern X` is `X in entry.name` (substring), not a regex (despite the docstring on
the Google Benchmark flag it ultimately wraps suggesting regex). Two-line
clarification in the help text would prevent confusion — I initially tried
`-k "n=10000"` and got no matches because the entry name doesn't contain the
case label.

## 7. `pause()` on `_MockState` is a no-op

`_MockState.pause()` returns `nullcontext()`. For memory profiling that's fine
— you want the setup measured too. For CPU profiling under
`mew.cpu.profile()` it's silently wrong: paused setup gets sampled. Worth a
docstring callout, or have the CPU profiler honor `pause()`.

## 8. Repo-relative imports break in bench files

`_discovery.import_file` (`src/mew/discovery.py:55`) doesn't add the file's
parent dir to `sys.path`, so `from _bench_fixtures import ...` fails unless
each bench file manually inserts the parent. Doable but every bench file in a
dir needs the boilerplate.

Fix idea: `sys.path.insert(0, str(path.parent))` inside `import_file` (and pop
it afterwards), matching pytest's conftest-style discovery.

# Writing and measuring benchmarks

Decide what one iteration should measure: sorting an existing list, parsing a message, or running a compiled function.
Input creation, cache state, and waiting for background work can each change the answer.

## The timed loop

A benchmark function receives a {class}`mew.State`.
Iterating it runs the timed loop, and code before and after the loop is excluded from timing.
One pass through the loop is an **iteration**.
Google Benchmark may call the function several times while choosing an iteration count, so setup must work on every call.
A **repetition** reruns the benchmark, warm-up included, to collect another measurement.
Result files keep every repetition, and comparisons reduce them to their median.

If input creation belongs in the operation you care about, put it inside the loop.
Constant expressions such as `2 + 3` may be folded by Python, leaving a loop that measures only itself.
Keep correctness checks outside the loop unless checking is itself part of the workload.

### Reset inputs that change

Repeatedly calling `data.sort()` mostly measures sorting an already sorted list.
`state.pause()` excludes its block from timing, so each iteration can get fresh input:

```python
@mew.benchmark
def bench_inplace_sort(state: mew.State) -> None:
    original = list(range(1000, 0, -1))
    for _ in state:
        with state.pause():
            data = original.copy()
        data.sort()
```

Pause only inside the loop, where outside it raises and skips the benchmark.
A pause resumes timing when its block raises, and a pause nested in another does nothing.
A pause costs tens of nanoseconds of real time and more of CPU time, so use it only around work that takes much longer.

### Batch very fast bodies

Each ordinary iteration crosses from Python into Google Benchmark.
For bodies under about 100 ns, `state.batches(n)` hands out iterations in batches, so the inner loop is plain Python:

```python
@mew.benchmark
def bench_add(state: mew.State) -> None:
    a, b = 1, 2
    for n in state.batches(1024):
        for _ in range(n):
            a + b
```

The last batch may exceed the iteration budget by up to `n - 1`, and per-iteration times use the actual count.
Batched and ordinary loops are not comparable, so keep one style per benchmark.

## Match the workload

Use input sizes, dtypes, and value distributions your application encounters.
A descending list exercises different behavior from a shuffled or almost sorted one.
Use [parametrized cases](python-api.md#inputs-names-and-tags) when these differences matter.

Repeated calls reuse caches and allocator state.
A memoized function may compute once and return cached values thereafter.
Decide whether you need the cached path, a cold computation, or both, and reset caches explicitly for a cold path.

For JIT-compiled code, compile and make an initial call before the loop when measuring warm execution.
For asynchronous work, wait for completion inside every timed iteration.
Otherwise you measure submission while the work continues in the background.
The [JAX history recipe](../recipes/git-history.md) shows a complete example.

## Clocks

mew reports wall-clock (`Real`) and CPU time.
Equal values mean single-threaded, CPU-bound work.
Real above CPU means the body sleeps, waits on I/O, or contends on a lock.
Google Benchmark uses CPU time to choose the iteration count by default.
Use `@mew.benchmark(use_real_time=True)` when elapsed time matters, such as waiting for a device or a worker thread.

For a clock of your own, such as a GPU timer, set `use_manual_time=True` and call `state.set_iteration_time(seconds)` once per iteration.
Manual time replaces measured real time and cannot be combined with `use_real_time`.

## Threads and counters

`threads=4` runs the body on four threads at once, and `threads=[1, 2, 4]` runs it at each count.
Each thread gets its own `State`, so use `state.threads` and `state.thread_index` to divide the work, and make the body thread-safe:

```python
@mew.benchmark(threads=[1, 2, 4])
def bench_parallel(state: mew.State) -> None:
    chunk = len(DATA) // state.threads
    part = DATA[state.thread_index * chunk : (state.thread_index + 1) * chunk]
    for _ in state:
        process(part)
```

Threaded benchmarks need a free-threaded interpreter, CPython 3.14t or later.
On a GIL build mew warns and leaves them out of the run, and `--strict` turns that into an error.
Profiling passes run one thread, where `state.threads` is 1.

`state.set_counter("hits", hits)` adds a counter to the result row, and `set_items_processed()` and `set_bytes_processed()` add rates.
Counters are summed across threads, so set per-thread values or guard a one-time update with `if state.thread_index == 0`.
Pass {class}`mew.CounterFlags` for rates and normalization.
To compare or gate on a counter, see [counters](comparing.md#counters).

## Run and select

```console
$ mew list --show-cases
$ mew run benchmarks/bench_sort.py
$ mew run -k 'sort|search' -t hot-path
$ mew run -F -k 'bench_sorted[n=1000]'
$ mew run 'benchmarks/bench_sort.py::n=1000'
$ mew list -k sort | mew run --stdin
```

`-k` searches a regular expression in benchmark names, and a match on some cases of a family runs only those.
`-F` matches literal text instead, so a listed `name[label]` works as written.
The same rules apply to the filter after `::`.
Path selectors combine with OR, the global `-k` narrows them with AND, and several `-t` tags combine with OR.
`--stdin` reads one selector per line, as `mew list` prints them, relative to the directory you listed from.

Output from benchmarked code, native writes included, is held back and printed to stderr after the run, so it cannot break up the table or JSON on stdout.
`--capture=no` shows it live when debugging a crash.
`--summary-only` hides individual repetitions from the terminal while result files keep them.

## Run length

```console
$ mew run --min-time 1s --min-warmup-time 200ms --repetitions 10 -o baseline.json
```

The default minimum time is half a second per benchmark, written as `0.5`, `500ms`, or `1m`.
Raising it reduces sampling error but not clock or load drift.
Warm-up runs before measurement.
To force an iteration count, use `iterations=100` on the decorator or `--min-time 100x`.
A fixed count cannot be combined with a time-based `min_time` or `min_warmup_time` on the same decorator.
Decorator options win over `mew run` flags, except that `--min-time 100x` also replaces a decorator's time-based `min_time`.

(trust-the-numbers)=
## Trust the numbers

A timing difference can come from the code, the machine, or ordinary variation.
Repeat the baseline before changing anything to see how much it moves.

**Keep the environment comparable.**
Use the same machine, interpreter, dependencies, and inputs on both sides.
mew records machine and interpreter details in every row and warns when compared files differ in CPU model, CPU count, or scaling, or in a `host` your suite records.
Treat those warnings as invalidating.
The header's `scaling=enabled` means CPU frequency can change during measurement.
On Linux, pin the governor to `performance` with `cpupower` or an equivalent.

**Collect repetitions and interleave them.**
`--random-interleaving` shuffles repetitions across benchmarks, so drift in load or temperature spreads over all of them.
It needs more than one repetition, and it does not alternate separate processes, which the [A/B how-to](../howto/ab-comparison.md) does.

**Read the spread.**
Each compared value carries its coefficient of variation, the standard deviation over the mean, as `±N%`:

```text
Benchmark   │      baseline │             head │                Δ% │ speedup
────────────────────────────────────────────────────────────────────────────
bench_parse │ 1.20 us ±3.1% │ 1.44 us ±26% (!) │           +20.00% │  ×0.833
bench_load  │ 3.10 us ±1.2% │ 3.72 us ±0.9%    │ +20.00% (signif.) │  ×0.833
```

Above 25% it is flagged `(!)`, so treat that delta as inconclusive and improve the environment or collect more repetitions.
With at least two repetitions per side, mew runs a Mann-Whitney U test and marks deltas with p < 0.05 as `(signif.)`, or with `▲` or `▼` in the compact layout.
With three per side nothing can be marked, and four per side is the fewest that can be.
A marker is evidence against equal distributions, and its absence is not evidence of equality.
A detectable difference may still be too small to matter to your application.

// State bindings: wraps benchmark::State for Python iteration and metrics.

#include <benchmark/benchmark.h>
#include <nanobind/nanobind.h>
#include <nanobind/stl/string.h>

#include <memory>

namespace nb = nanobind;
using namespace nb::literals;

namespace {
// ScopedPauseTiming is non-movable; hold it behind unique_ptr to defer
// construction until __enter__.
struct PauseScope {
    benchmark::State* state;
    std::unique_ptr<benchmark::ScopedPauseTiming> guard;
};

struct BatchIter {
    benchmark::State* state;
    int64_t n;
};
}  // namespace

void register_state(nb::module_& m) {
    nb::enum_<benchmark::Counter::Flags>(m, "CounterFlags", nb::is_arithmetic(), nb::is_flag(),
                                         "Flags forwarded to `benchmark::Counter`.\n"
                                         "OR together to combine (e.g. `kIsRate | kInvert`).")
        .value("kDefaults", benchmark::Counter::kDefaults)
        .value("kIsRate", benchmark::Counter::kIsRate)
        .value("kAvgThreads", benchmark::Counter::kAvgThreads)
        .value("kAvgThreadsRate", benchmark::Counter::kAvgThreadsRate)
        .value("kIsIterationInvariant", benchmark::Counter::kIsIterationInvariant)
        .value("kIsIterationInvariantRate", benchmark::Counter::kIsIterationInvariantRate)
        .value("kAvgIterations", benchmark::Counter::kAvgIterations)
        .value("kAvgIterationsRate", benchmark::Counter::kAvgIterationsRate)
        .value("kInvert", benchmark::Counter::kInvert);

    nb::class_<BatchIter>(m, "BatchIter", "Iterator yielding batch sizes from `State.batches`.")
        .def(
            "__iter__", [](BatchIter& self) -> BatchIter& { return self; },
            nb::rv_policy::reference_internal)
        .def("__next__", [](BatchIter& self) {
            if (!self.state->KeepRunningBatch(self.n)) throw nb::stop_iteration();
            return self.n;
        });

    nb::class_<PauseScope>(m, "PauseScope",
                           "Context manager that pauses State timing within a scope.")
        .def(
            "__enter__",
            [](PauseScope& self) -> PauseScope& {
                self.guard = std::make_unique<benchmark::ScopedPauseTiming>(*self.state);
                return self;
            },
            nb::rv_policy::reference_internal, nb::sig("def __enter__(self) -> typing.Self"))
        .def(
            "__exit__",
            [](PauseScope& self, nb::object, nb::object, nb::object) { self.guard.reset(); },
            "exc_type"_a.none(), "exc_value"_a.none(), "traceback"_a.none(),
            nb::sig("def __exit__(self, exc_type: type[BaseException] | None, exc_value: "
                    "BaseException | None, traceback: types.TracebackType | None) -> None"));

    nb::class_<benchmark::State>(m, "State",
                                 "Active microbenchmark state.\n"
                                 "Iterate with `for _ in state:` to time the body.")
        .def(
            "__iter__", [](benchmark::State& self) -> benchmark::State& { return self; },
            nb::rv_policy::reference_internal)
        .def("__next__",
             [](benchmark::State& self) {
                 if (!self.KeepRunning()) throw nb::stop_iteration();
             })
        .def(
            "keep_running_batch",
            [](benchmark::State& self, int64_t n) {
                if (n <= 0) throw nb::value_error("batch size must be positive");
                return self.KeepRunningBatch(n);
            },
            "n"_a,
            "Advance the iteration counter by `n`; return whether the budget permits another "
            "batch.\n"
            "Prefer `State.batches` for the idiomatic loop form.")
        .def(
            "batches",
            [](benchmark::State& self, int64_t n) {
                if (n <= 0) throw nb::value_error("batch size must be positive");
                return BatchIter{&self, n};
            },
            nb::keep_alive<0, 1>(), "n"_a,
            "Return an iterator yielding `n` once per batch until the budget is spent.\n"
            "Use with a nested `for _ in range(n)` to amortize `__next__` dispatch for very fast "
            "bodies.\n"
            "Reported times include a small per-batch overshoot; do not mix with `for _ in state` "
            "results.")
        .def(
            "pause", [](benchmark::State& self) { return PauseScope{&self, nullptr}; },
            nb::keep_alive<0, 1>(),
            "Return a context manager that pauses timing for the duration of the `with` block.")
        .def("skip_with_error", &benchmark::State::SkipWithError, "msg"_a,
             "Abort this benchmark and mark it failed; the row reports as skipped.")
        .def("skip_with_message", &benchmark::State::SkipWithMessage, "msg"_a,
             "Abort this benchmark without marking it an error (e.g. an unmet "
             "precondition).")
        .def("set_label", &benchmark::State::SetLabel, "label"_a,
             "Attach a free-form label to this benchmark's reported row.")
        .def("set_iteration_time", &benchmark::State::SetIterationTime, "seconds"_a,
             "Report the elapsed time of one iteration yourself. Only honored when "
             "the benchmark sets `use_manual_time`.")
        .def("set_items_processed", &benchmark::State::SetItemsProcessed, "items"_a,
             "Record how many items the body handled, reported as items/second.")
        .def("set_bytes_processed", &benchmark::State::SetBytesProcessed, "n_bytes"_a,
             "Record how many bytes the body handled, reported as bytes/second.")
        .def(
            "set_counter",
            [](benchmark::State& self, const std::string& name, double value,
               benchmark::Counter::Flags flags) {
                self.counters[name] = benchmark::Counter(value, flags);
            },
            "name"_a, "value"_a,
            // Spell the default symbolically. nanobind renders defaults with
            // PyObject_Repr, and since 3.11 every IntFlag member reprs as a bare
            // int (enum.ReprEnum inherits int.__repr__), so this would read `= 0`.
            "flags"_a.sig("CounterFlags.kDefaults") = benchmark::Counter::kDefaults,
            "Attach a user-defined counter, surfaced in `RunRow['counters']`.\n"
            "`flags` controls how Google Benchmark normalizes it (see `CounterFlags`).")
        .def(
            "range",
            [](const benchmark::State& self, std::size_t pos) {
                // GB's own guard is an assert, compiled out in release builds;
                // an unchecked call would read out of bounds.
                if (pos >= self.range_size()) {
                    throw nb::index_error(("range(" + std::to_string(pos) +
                                           ") out of bounds: benchmark has " +
                                           std::to_string(self.range_size()) + " range argument(s)")
                                              .c_str());
                }
                return self.range(pos);
            },
            "pos"_a = 0,
            "The `pos`-th range argument this benchmark was registered with.\n"
            "`@parametrize` / `@product` families use `range(0)` as the case index; "
            "the trampoline reads it to bind that case's kwargs and label.")
        .def_prop_ro("range_size", &benchmark::State::range_size,
                     "Number of range arguments available to `range`.")
        .def_prop_ro("iterations", &benchmark::State::iterations,
                     "Iterations completed so far; the total once the loop finishes.")
        .def_prop_ro("threads", &benchmark::State::threads,
                     "Total number of threads in this run (1 unless threaded mode is on).")
        .def_prop_ro("thread_index", &benchmark::State::thread_index,
                     "Index of the thread owning this State, in `[0, threads)`.")
        .def_prop_ro("name", &benchmark::State::name, "The registered benchmark name.")
        .def_prop_ro("skipped", &benchmark::State::skipped,
                     "Whether this benchmark was skipped, by either skip_with_* call.")
        .def_prop_ro("error_occurred", &benchmark::State::error_occurred,
                     "Whether the skip came from `skip_with_error` rather than "
                     "`skip_with_message`.")
        .def_prop_ro(
            "max_iterations", [](const benchmark::State& s) { return s.max_iterations; },
            "Iteration count Google Benchmark budgeted for this run.");
}

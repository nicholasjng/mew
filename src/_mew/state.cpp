// Python interface to benchmark::State.

#include <benchmark/benchmark.h>
#include <nanobind/nanobind.h>
#include <nanobind/stl/string.h>

#include <stdexcept>

#include "managers.h"

namespace nb = nanobind;
using namespace nb::literals;

namespace {
// The scope that stops the timer owns the pause; scopes nested inside it find
// the timer stopped and do nothing. `depth` counts re-entries of the owner.
struct PauseScope {
    benchmark::State* state;
    unsigned depth;
};

struct BatchIter {
    benchmark::State* state;
    int64_t n;
};

// Loop steps run through the iterator type slots: CPython calls them directly,
// skipping the `__next__` lookup and nanobind's argument dispatch, which cost
// about two thirds of an empty loop iteration. NULL without an exception set
// ends the loop.
PyObject* state_iternext(PyObject* self) {
    if (nb::inst_ptr<benchmark::State>(self)->KeepRunning()) return Py_NewRef(Py_None);
    return nullptr;
}

PyObject* batch_iternext(PyObject* self) {
    BatchIter* it = nb::inst_ptr<BatchIter>(self);
    if (it->state->KeepRunningBatch(it->n)) return PyLong_FromLongLong(it->n);
    return nullptr;
}

PyType_Slot state_slots[] = {
    {Py_tp_iter, (void*)PyObject_SelfIter}, {Py_tp_iternext, (void*)state_iternext}, {0, nullptr}};
PyType_Slot batch_slots[] = {
    {Py_tp_iter, (void*)PyObject_SelfIter}, {Py_tp_iternext, (void*)batch_iternext}, {0, nullptr}};
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

    nb::enum_<benchmark::Counter::OneK>(m, "CounterOneK",
                                        "Base used to scale a counter for display.")
        .value("kIs1000", benchmark::Counter::kIs1000)
        .value("kIs1024", benchmark::Counter::kIs1024);

    nb::class_<BatchIter>(m, "BatchIter", nb::type_slots(batch_slots),
                          "Iterator yielding batch sizes from `State.batches`.");

    nb::class_<PauseScope>(m, "PauseScope",
                           "Context manager that pauses State timing within a scope.")
        .def(
            "__enter__",
            [](PauseScope& self) -> PauseScope& {
                if (!self.state->in_timed_section()) {
                    throw std::runtime_error(
                        "state.pause() is only valid inside the benchmark loop");
                }
                if (self.depth > 0) {
                    ++self.depth;
                } else if (self.state->timer_running()) {
                    self.state->PauseTiming();
                    // Match CPU sampling to the timed region. A no-op outside
                    // the single-threaded profiler pass.
                    mew_profiler_pause();
                    self.depth = 1;
                }
                return self;
            },
            nb::rv_policy::reference_internal, nb::sig("def __enter__(self) -> typing.Self"))
        .def(
            "__exit__",
            [](PauseScope& self, nb::object, nb::object, nb::object) {
                // Ignore a nested scope, or an unmatched direct __exit__ call.
                if (self.depth == 0 || --self.depth != 0) return;
                mew_profiler_resume();
                // As in ScopedPauseTiming: a skip inside the block ends the loop, and
                // its timer must stay stopped.
                if (self.state->in_timed_section()) self.state->ResumeTiming();
            },
            "exc_type"_a.none(), "exc_value"_a.none(), "traceback"_a.none(),
            nb::sig("def __exit__(self, exc_type: type[BaseException] | None, exc_value: "
                    "BaseException | None, traceback: types.TracebackType | None) -> None"));

    nb::class_<benchmark::State>(m, "State", nb::type_slots(state_slots),
                                 "Active microbenchmark state.\n"
                                 "Iterate with `for _ in state:` to time the body.")
        .def(
            "keep_running_batch",
            [](benchmark::State& self, int64_t n) {
                if (n <= 0) throw nb::value_error("batch size must be positive");
                return self.KeepRunningBatch(n);
            },
            "n"_a, "Advance by `n` iterations and return whether another batch should run.")
        .def(
            "batches",
            [](benchmark::State& self, int64_t n) {
                if (n <= 0) throw nb::value_error("batch size must be positive");
                return BatchIter{&self, n};
            },
            "n"_a,
            "Iterate in batches of `n`, reducing dispatch overhead for fast bodies.\n"
            "The final batch may exceed the iteration budget.")
        .def(
            "pause", [](benchmark::State& self) { return PauseScope{&self, 0}; },
            "Return a context manager that pauses timing for the duration of the `with` block.\n"
            "Nested scopes resume timing only when the outermost scope exits.")
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
               benchmark::Counter::Flags flags, benchmark::Counter::OneK one_k) {
                self.counters[name] = benchmark::Counter(value, flags, one_k);
            },
            "name"_a, "value"_a,
            // Keep enum names in generated signatures.
            "flags"_a.sig("CounterFlags.kDefaults") = benchmark::Counter::kDefaults,
            "one_k"_a.sig("CounterOneK.kIs1000") = benchmark::Counter::kIs1000,
            "Attach a user-defined counter, surfaced in `BenchmarkResult['counters']`.\n"
            "`flags` controls normalization; `one_k` selects decimal or binary scaling.")
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
            "pos"_a = 0, "Return the range argument at `pos`.")
        .def_prop_ro("range_size", &benchmark::State::range_size,
                     "Number of range arguments available to `range`.")
        .def_prop_ro("iterations", &benchmark::State::iterations,
                     "Iterations completed so far; the total once the loop finishes.")
        .def_prop_ro("threads", &benchmark::State::threads,
                     "Threads in this run: 1 without threaded mode or in a profiling pass.")
        .def_prop_ro("thread_index", &benchmark::State::thread_index,
                     "Index of the thread owning this State, in `[0, threads)`.")
        .def_prop_ro("name", &benchmark::State::name, "The registered benchmark name.")
        .def_prop_ro("skipped", &benchmark::State::skipped,
                     "Whether this benchmark was skipped, by either skip_with_* call.")
        .def_prop_ro("error_occurred", &benchmark::State::error_occurred,
                     "Whether the skip came from `skip_with_error` rather than "
                     "`skip_with_message`.")
        .def_ro("max_iterations", &benchmark::State::max_iterations,
                "Iteration count Google Benchmark budgeted for this run.");
}

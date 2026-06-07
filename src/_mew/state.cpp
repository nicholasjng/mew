// State bindings — wraps benchmark::State for Python iteration and metrics.

#include <benchmark/benchmark.h>
#include <nanobind/nanobind.h>
#include <nanobind/stl/string.h>

#include <memory>

namespace nb = nanobind;
using namespace nb::literals;

namespace {
// Python context manager wrapping benchmark::ScopedPauseTiming. The guard is
// constructed on __enter__ (which calls PauseTiming) and destroyed on __exit__
// (which calls ResumeTiming). ScopedPauseTiming is non-movable, so we hold it
// behind a unique_ptr to defer construction until __enter__.
struct PauseScope {
    benchmark::State* state;
    std::unique_ptr<benchmark::ScopedPauseTiming> guard;
};
}  // namespace

void register_state(nb::module_& m) {
    nb::enum_<benchmark::Counter::Flags>(m, "CounterFlags", nb::is_arithmetic(),
                                         "Flags forwarded to `benchmark::Counter`. OR them "
                                         "together to combine (e.g. `kIsRate | kInvert`).")
        .value("kDefaults", benchmark::Counter::kDefaults)
        .value("kIsRate", benchmark::Counter::kIsRate)
        .value("kAvgThreads", benchmark::Counter::kAvgThreads)
        .value("kAvgThreadsRate", benchmark::Counter::kAvgThreadsRate)
        .value("kIsIterationInvariant", benchmark::Counter::kIsIterationInvariant)
        .value("kIsIterationInvariantRate", benchmark::Counter::kIsIterationInvariantRate)
        .value("kAvgIterations", benchmark::Counter::kAvgIterations)
        .value("kAvgIterationsRate", benchmark::Counter::kAvgIterationsRate)
        .value("kInvert", benchmark::Counter::kInvert);

    nb::class_<PauseScope>(m, "PauseScope",
                           "Context manager that pauses State timing within a scope.")
        .def(
            "__enter__",
            [](PauseScope& self) -> PauseScope& {
                self.guard = std::make_unique<benchmark::ScopedPauseTiming>(*self.state);
                return self;
            },
            nb::rv_policy::reference_internal)
        .def(
            "__exit__",
            [](PauseScope& self, nb::object, nb::object, nb::object) { self.guard.reset(); },
            "exc_type"_a.none(), "exc_value"_a.none(), "traceback"_a.none(),
            nb::sig("def __exit__(self, exc_type: type[BaseException] | None, exc_value: "
                    "BaseException | None, traceback: types.TracebackType | None) -> None"));

    nb::class_<benchmark::State>(
        m, "State", "Active microbenchmark state. Iterate with `for _ in state:` to time the body.")
        .def(
            "__iter__", [](benchmark::State& self) -> benchmark::State& { return self; },
            nb::rv_policy::reference_internal)
        .def("__next__",
             [](benchmark::State& self) {
                 // KeepRunning() lazily starts the timer on first call, decrements the
                 // internal counter, and calls FinishKeepRunning when the budget is spent.
                 if (!self.KeepRunning()) throw nb::stop_iteration();
             })
        .def(
            "pause", [](benchmark::State& self) { return PauseScope{&self, nullptr}; },
            nb::keep_alive<0, 1>(),
            "Return a context manager that pauses timing for the duration of the `with` block.")
        .def("skip_with_error", &benchmark::State::SkipWithError, "msg"_a)
        .def("skip_with_message", &benchmark::State::SkipWithMessage, "msg"_a)
        .def("set_label", &benchmark::State::SetLabel, "label"_a)
        .def("set_iteration_time", &benchmark::State::SetIterationTime, "seconds"_a)
        .def("set_items_processed", &benchmark::State::SetItemsProcessed, "items"_a)
        .def("set_bytes_processed", &benchmark::State::SetBytesProcessed, "n_bytes"_a)
        .def(
            "set_counter",
            [](benchmark::State& self, const std::string& name, double value,
               benchmark::Counter::Flags flags) {
                self.counters[name] = benchmark::Counter(value, flags);
            },
            "name"_a, "value"_a, "flags"_a = benchmark::Counter::kDefaults)
        .def("range", &benchmark::State::range, "pos"_a = 0)
        .def_prop_ro("range_size", &benchmark::State::range_size)
        .def_prop_ro("iterations", &benchmark::State::iterations)
        .def_prop_ro("threads", &benchmark::State::threads)
        .def_prop_ro("thread_index", &benchmark::State::thread_index)
        .def_prop_ro("name", &benchmark::State::name)
        .def_prop_ro("skipped", &benchmark::State::skipped)
        .def_prop_ro("error_occurred", &benchmark::State::error_occurred)
        .def_prop_ro("max_iterations", [](const benchmark::State& s) { return s.max_iterations; });
}

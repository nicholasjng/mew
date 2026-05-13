// State bindings — wraps benchmark::State for Python iteration and metrics.

#include <benchmark/benchmark.h>
#include <nanobind/nanobind.h>
#include <nanobind/stl/string.h>

namespace nb = nanobind;
using namespace nb::literals;

void register_state(nb::module_& m) {
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
        .def("pause_timing", &benchmark::State::PauseTiming)
        .def("resume_timing", &benchmark::State::ResumeTiming)
        .def("skip_with_error", &benchmark::State::SkipWithError, "msg"_a)
        .def("skip_with_message", &benchmark::State::SkipWithMessage, "msg"_a)
        .def("set_label", &benchmark::State::SetLabel, "label"_a)
        .def("set_iteration_time", &benchmark::State::SetIterationTime, "seconds"_a)
        .def("set_items_processed", &benchmark::State::SetItemsProcessed, "items"_a)
        .def("set_bytes_processed", &benchmark::State::SetBytesProcessed, "n_bytes"_a)
        .def(
            "set_counter",
            [](benchmark::State& self, const std::string& name, double value) {
                self.counters[name] = benchmark::Counter(value);
            },
            "name"_a, "value"_a)
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

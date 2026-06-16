// Registry bindings — register a Python callable as a Google Benchmark,
// exposing the Benchmark* as a chainable handle.

#include <benchmark/benchmark.h>
#include <nanobind/nanobind.h>
#include <nanobind/stl/string.h>

#include <memory>
#include <string>

namespace nb = nanobind;
using namespace nb::literals;

void register_registry(nb::module_& m) {
    nb::class_<benchmark::Benchmark>(
        m, "BenchmarkHandle",
        "Handle to a registered Google Benchmark.\n"
        "Methods return the same handle so options can be chained.\n"
        "Invalidated by the next `clear_registered_benchmarks()` call or "
        "interpreter shutdown; using a stale handle is undefined behaviour.")
        .def("min_time", &benchmark::Benchmark::MinTime, "seconds"_a, nb::rv_policy::reference)
        .def("min_warmup_time", &benchmark::Benchmark::MinWarmUpTime, "seconds"_a,
             nb::rv_policy::reference)
        .def("iterations", &benchmark::Benchmark::Iterations, "n"_a, nb::rv_policy::reference)
        .def("repetitions", &benchmark::Benchmark::Repetitions, "n"_a, nb::rv_policy::reference)
        .def("unit", &benchmark::Benchmark::Unit, "unit"_a, nb::rv_policy::reference)
        .def("use_real_time", &benchmark::Benchmark::UseRealTime, nb::rv_policy::reference)
        .def("use_manual_time", &benchmark::Benchmark::UseManualTime, nb::rv_policy::reference)
        .def("measure_process_cpu_time", &benchmark::Benchmark::MeasureProcessCPUTime,
             nb::rv_policy::reference)
        .def("report_aggregates_only", &benchmark::Benchmark::ReportAggregatesOnly,
             "value"_a = true, nb::rv_policy::reference)
        .def("display_aggregates_only", &benchmark::Benchmark::DisplayAggregatesOnly,
             "value"_a = true, nb::rv_policy::reference)
        .def("dense_range", &benchmark::Benchmark::DenseRange, "start"_a, "limit"_a, "step"_a = 1,
             nb::rv_policy::reference)
        .def("threads", &benchmark::Benchmark::Threads, "n"_a, nb::rv_policy::reference,
             "Run the benchmark with `n` threads, each with its own State and timer.\n"
             "Requires a free-threaded interpreter (CPython 3.13t+): under the GIL the "
             "trampoline holds the GIL across Google Benchmark's per-thread start "
             "barrier, so the workers deadlock rather than run. On a GIL build mew "
             "warns and skips threaded benchmarks by default (see mew.run).")
        .def("thread_range", &benchmark::Benchmark::ThreadRange, "min_threads"_a, "max_threads"_a,
             nb::rv_policy::reference,
             "Run the benchmark once per thread count in [min_threads, max_threads], "
             "stepping by the range multiplier (powers of two). See `threads` for the "
             "free-threading requirement.")
        .def("thread_per_cpu", &benchmark::Benchmark::ThreadPerCpu, nb::rv_policy::reference,
             "Run the benchmark with one thread per CPU. See `threads` for the "
             "free-threading requirement.")
        .def("arg", &benchmark::Benchmark::Arg, "value"_a, nb::rv_policy::reference)
        .def("arg_name", &benchmark::Benchmark::ArgName, "name"_a, nb::rv_policy::reference)
        .def_prop_ro("name", &benchmark::Benchmark::GetName);

    m.def(
        "register_benchmark",
        [](const std::string& name, nb::callable fn) -> benchmark::Benchmark* {
            // Wrap the Python callable in a shared_ptr so the lambda is copyable
            // (Google Benchmark stores it as a std::function).
            auto holder = std::make_shared<nb::callable>(std::move(fn));
            return benchmark::RegisterBenchmark(name, [holder](benchmark::State& s) {
                nb::gil_scoped_acquire gil;
                try {
                    (*holder)(nb::cast(&s, nb::rv_policy::reference));
                } catch (nb::python_error& e) {
                    // SkipWithError captures the traceback; discard the Python
                    // error so it doesn't leak into the next benchmark.
                    s.SkipWithError(e.what());
                    e.discard_as_unraisable(*holder);
                } catch (std::exception& e) {
                    s.SkipWithError(e.what());
                }
            });
        },
        "name"_a, "fn"_a, nb::rv_policy::reference,
        "Register `fn` as a benchmark under `name` and return a chainable handle.");

    m.def("clear_registered_benchmarks", &benchmark::ClearRegisteredBenchmarks,
          "Drop all previously registered benchmarks from the global registry.");
}

// Registry bindings — register a Python callable as a Google Benchmark,
// exposing the Benchmark* as a chainable handle.

#include <benchmark/benchmark.h>
#include <nanobind/nanobind.h>
#include <nanobind/stl/string.h>

#include <memory>
#include <string>

namespace nb = nanobind;
using namespace nb::literals;

namespace {

benchmark::TimeUnit parse_unit(const std::string& u) {
    if (u == "ns") return benchmark::kNanosecond;
    if (u == "us") return benchmark::kMicrosecond;
    if (u == "ms") return benchmark::kMillisecond;
    if (u == "s") return benchmark::kSecond;
    throw nb::value_error(("unknown time unit: " + u).c_str());
}

}  // namespace

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
        .def(
            "unit",
            [](benchmark::Benchmark& b, const std::string& u) { return b.Unit(parse_unit(u)); },
            "unit"_a, nb::rv_policy::reference,
            nb::sig(
                "def unit(self, unit: typing.Literal['ns', 'us', 'ms', 's']) -> BenchmarkHandle"))
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

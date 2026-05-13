// Registry bindings — register a Python callable as a Google Benchmark and
// expose the resulting Benchmark* as a chainable handle.

#include <benchmark/benchmark.h>
#include <nanobind/nanobind.h>
#include <nanobind/stl/string.h>

#include <memory>
#include <stdexcept>
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
        "Handle to a registered Google Benchmark. Methods return the same handle "
        "so options can be chained.")
        .def(
            "min_time", [](benchmark::Benchmark& b, double t) { return b.MinTime(t); }, "seconds"_a,
            nb::rv_policy::reference)
        .def(
            "min_warmup_time", [](benchmark::Benchmark& b, double t) { return b.MinWarmUpTime(t); },
            "seconds"_a, nb::rv_policy::reference)
        .def(
            "iterations", [](benchmark::Benchmark& b, int64_t n) { return b.Iterations(n); }, "n"_a,
            nb::rv_policy::reference)
        .def(
            "repetitions", [](benchmark::Benchmark& b, int n) { return b.Repetitions(n); }, "n"_a,
            nb::rv_policy::reference)
        .def(
            "unit",
            [](benchmark::Benchmark& b, const std::string& u) { return b.Unit(parse_unit(u)); },
            "unit"_a, nb::rv_policy::reference)
        .def(
            "use_real_time", [](benchmark::Benchmark& b) { return b.UseRealTime(); },
            nb::rv_policy::reference)
        .def(
            "use_manual_time", [](benchmark::Benchmark& b) { return b.UseManualTime(); },
            nb::rv_policy::reference)
        .def(
            "measure_process_cpu_time",
            [](benchmark::Benchmark& b) { return b.MeasureProcessCPUTime(); },
            nb::rv_policy::reference)
        .def(
            "report_aggregates_only",
            [](benchmark::Benchmark& b, bool v) { return b.ReportAggregatesOnly(v); },
            "value"_a = true, nb::rv_policy::reference)
        .def(
            "display_aggregates_only",
            [](benchmark::Benchmark& b, bool v) { return b.DisplayAggregatesOnly(v); },
            "value"_a = true, nb::rv_policy::reference)
        .def_prop_ro("name", [](benchmark::Benchmark& b) { return std::string(b.GetName()); });

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
                    s.SkipWithError(e.what());
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

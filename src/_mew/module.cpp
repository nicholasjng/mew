// nanobind module entry point: wires up the three binding groups.

#include <benchmark/benchmark.h>
#include <nanobind/nanobind.h>
#include <nanobind/stl/string.h>

#ifndef MEW_BENCHMARK_COMMIT
#define MEW_BENCHMARK_COMMIT "unknown"
#endif

namespace nb = nanobind;

void register_state(nb::module_& m);
void register_registry(nb::module_& m);
void register_reporter(nb::module_& m);

NB_MODULE(_core, m) {
    m.doc() = "The mew C++ core (Google Benchmark bindings).";

    m.attr("BENCHMARK_COMMIT") = MEW_BENCHMARK_COMMIT;
    m.attr("BENCHMARK_VERSION") = benchmark::GetBenchmarkVersion();

    // Reporter first so registry/run_benchmarks can reference Run and TimeUnit.
    register_reporter(m);
    register_state(m);
    register_registry(m);
}

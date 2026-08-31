// Registry bindings: register a Python callable as a Google Benchmark,
// exposing the Benchmark* as a chainable handle.

#include <benchmark/benchmark.h>
#include <nanobind/nanobind.h>
#include <nanobind/stl/string.h>

#include <exception>
#include <memory>
#include <string>

#include "abort.h"

namespace nb = nanobind;
using namespace nb::literals;

void register_registry(nb::module_& m) {
    nb::class_<benchmark::Benchmark>(
        m, "BenchmarkHandle",
        "Handle to a registered Google Benchmark.\n"
        "Methods return the same handle so options can be chained.\n"
        "Invalidated by the next `clear_registered_benchmarks()` call or "
        "interpreter shutdown; using a stale handle is undefined behaviour.")
        .def("min_time", &benchmark::Benchmark::MinTime, "seconds"_a, nb::rv_policy::reference,
             "Run at least this many seconds before reporting.")
        .def("min_warmup_time", &benchmark::Benchmark::MinWarmUpTime, "seconds"_a,
             nb::rv_policy::reference, "Warm up for this many seconds before measuring.")
        .def("iterations", &benchmark::Benchmark::Iterations, "n"_a, nb::rv_policy::reference,
             "Run exactly `n` iterations instead of timing out on `min_time`.")
        .def("repetitions", &benchmark::Benchmark::Repetitions, "n"_a, nb::rv_policy::reference,
             "Repeat the whole benchmark `n` times; variance metrics need at least 2.")
        .def("unit", &benchmark::Benchmark::Unit, "unit"_a, nb::rv_policy::reference,
             "Time unit for the reported per-iteration durations.")
        .def("use_real_time", &benchmark::Benchmark::UseRealTime, nb::rv_policy::reference,
             "Report wall-clock rather than CPU time as the primary measure.")
        .def("use_manual_time", &benchmark::Benchmark::UseManualTime, nb::rv_policy::reference,
             "Take timings from `State.set_iteration_time` instead of the built-in timer.")
        .def("measure_process_cpu_time", &benchmark::Benchmark::MeasureProcessCPUTime,
             nb::rv_policy::reference,
             "Measure CPU time across the whole process, not just the running thread.")
        .def("report_aggregates_only", &benchmark::Benchmark::ReportAggregatesOnly,
             "value"_a = true, nb::rv_policy::reference,
             "Emit only the aggregate rows, suppressing per-repetition ones.")
        .def("dense_range", &benchmark::Benchmark::DenseRange, "start"_a, "limit"_a, "step"_a = 1,
             nb::rv_policy::reference,
             "Register one case per value in `[start, limit]`, readable via `State.range`.")
        .def("threads", &benchmark::Benchmark::Threads, "n"_a, nb::rv_policy::reference,
             "Run the benchmark with `n` threads, each with its own State and timer.\n"
             "Requires a free-threaded interpreter: under the GIL the "
             "trampoline holds the GIL across Google Benchmark's per-thread start "
             "barrier, so the workers deadlock rather than run. On a GIL build mew "
             "warns and skips threaded benchmarks by default (see mew.run).")
        .def("thread_range", &benchmark::Benchmark::ThreadRange, "min_threads"_a, "max_threads"_a,
             nb::rv_policy::reference,
             "Run the benchmark once per thread count in [min_threads, max_threads], "
             "stepping by the range multiplier (powers of two). See `threads` for the "
             "free-threading requirement.")
        .def("dense_thread_range", &benchmark::Benchmark::DenseThreadRange, "min_threads"_a,
             "max_threads"_a, "stride"_a = 1, nb::rv_policy::reference,
             "Run once per thread count in [min_threads, max_threads], stepping by stride.\n"
             "See `threads` for the free-threading requirement.")
        .def("arg", &benchmark::Benchmark::Arg, "value"_a, nb::rv_policy::reference,
             "Register one case with a single range argument, readable via `State.range`.")
        .def("arg_name", &benchmark::Benchmark::ArgName, "name"_a, nb::rv_policy::reference,
             "Name the first range argument; appears in the reported benchmark name.")
        .def_prop_ro("name", &benchmark::Benchmark::GetName, "The registered benchmark name.");

    m.def(
        "register_benchmark",
        [](const std::string& name, nb::callable fn) -> benchmark::Benchmark* {
            // Wrap the Python callable in a shared_ptr so the lambda is copyable
            // (Google Benchmark stores it as a std::function).
            auto holder = std::make_shared<nb::callable>(std::move(fn));
            return benchmark::RegisterBenchmark(name, [holder](benchmark::State& s) {
                // The run is already aborting (a Ctrl-C, or a reporter/manager
                // that raised): wind down without touching Python again.
                if (mew_abort_pending()) {
                    s.SkipWithError("aborted");
                    return;
                }
                nb::gil_scoped_acquire gil;
                try {
                    (*holder)(nb::cast(&s, nb::rv_policy::reference));
                } catch (nb::python_error& e) {
                    if (!e.matches(PyExc_Exception)) {
                        // BaseException-only (KeyboardInterrupt, SystemExit) must
                        // stop the whole run, not skip one benchmark.
                        s.SkipWithError(e.what());
                        mew_set_pending_abort(std::current_exception());
                        return;
                    }
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

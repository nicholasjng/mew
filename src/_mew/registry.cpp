// Register Python callables with Google Benchmark.

#include <benchmark/benchmark.h>
#include <nanobind/nanobind.h>
#include <nanobind/stl/string.h>

#include <exception>
#include <string>
#include <utility>

#include "abort.h"

namespace nb = nanobind;
using namespace nb::literals;

namespace {

// Only use the formatted exception as skip message, not the full traceback.
std::string skip_message(const nb::python_error& e) noexcept {
    std::string message = "the benchmark raised an exception";
    try {
        nb::module_ traceback = nb::module_::import_("traceback");
        nb::object lines = traceback.attr("format_exception_only")(e.value());
        message = nb::cast<std::string>(nb::str("").attr("join")(lines).attr("rstrip")());
        // Last, so that a stderr that can't be written to keeps the message.
        traceback.attr("print_exception")(e.value());
    } catch (const std::exception&) {
    }
    return message;
}

// Apply mew's normalized benchmark options (see mew._options.normalize_options).
// Keys absent from `options` keep Google Benchmark's defaults.
void apply_options(benchmark::Benchmark* b, const nb::dict& options) {
    auto get = [&](const char* key) -> nb::handle {
        return options.contains(key) ? nb::handle(options[key]) : nb::handle();
    };
    if (auto v = get("min_time")) b->MinTime(nb::cast<double>(v));
    if (auto v = get("min_warmup_time")) b->MinWarmUpTime(nb::cast<double>(v));
    if (auto v = get("iterations")) b->Iterations(nb::cast<benchmark::IterationCount>(v));
    if (auto v = get("repetitions")) b->Repetitions(nb::cast<int>(v));
    if (auto v = get("use_real_time"); v && nb::cast<bool>(v)) b->UseRealTime();
    if (auto v = get("use_manual_time"); v && nb::cast<bool>(v)) b->UseManualTime();
    if (auto v = get("measure_process_cpu_time"); v && nb::cast<bool>(v)) {
        b->MeasureProcessCPUTime();
    }
    // GB accumulates: one Threads() call per count runs the benchmark once each.
    if (auto v = get("threads")) {
        for (nb::handle n : v) b->Threads(nb::cast<int>(n));
    }
}

}  // namespace

void register_registry(nb::module_& m) {
    m.def(
        "register_benchmark",
        [](const std::string& name, nb::callable fn, nb::dict options) {
            // GB owns the lambda until clear_registered_benchmarks(). The Benchmark
            // pointer stays here, so Python never holds one that a clear invalidates.
            benchmark::Benchmark* b =
                benchmark::RegisterBenchmark(name, [fn = std::move(fn)](benchmark::State& s) {
                    // Avoid Python calls while an abort is pending.
                    if (mew_abort_pending()) {
                        s.SkipWithError("aborted");
                        return;
                    }
                    nb::gil_scoped_acquire gil;
                    try {
                        fn(nb::cast(&s, nb::rv_policy::reference));
                        // A return or break in the final iteration leaves the timer
                        // running with a complete iteration count, which GB would
                        // report as a near-zero time.
                        if (s.in_timed_section()) {
                            s.SkipWithError("The benchmark did not complete its loop.");
                        }
                    } catch (nb::python_error& e) {
                        s.SkipWithError(skip_message(e));
                        // BaseException-only (KeyboardInterrupt, SystemExit) must
                        // stop the whole run, not skip one benchmark.
                        if (!e.matches(PyExc_Exception)) {
                            mew_set_pending_abort(std::current_exception());
                        }
                    } catch (std::exception& e) {
                        s.SkipWithError(e.what());
                    }
                });
            apply_options(b, options);
        },
        "name"_a, "fn"_a, "options"_a = nb::dict(),
        "Register `fn` as a benchmark under `name`, with mew's normalized benchmark options.");

    m.def("clear_registered_benchmarks", &benchmark::ClearRegisteredBenchmarks,
          "Drop all previously registered benchmarks from the global registry.");
}

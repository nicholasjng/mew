// Reporter bindings — exposes Run/Context as Python objects, wires a duck-typed
// Python reporter object into Google Benchmark's BenchmarkReporter interface,
// and exposes `run_benchmarks` as the runner entry point.

#include <benchmark/benchmark.h>
#include <nanobind/nanobind.h>
#include <nanobind/stl/string.h>
#include <nanobind/stl/vector.h>

#include <exception>
#include <memory>
#include <string>
#include <vector>

namespace nb = nanobind;
using namespace nb::literals;

using benchmark::BenchmarkReporter;
using Run = BenchmarkReporter::Run;
using Context = BenchmarkReporter::Context;

namespace {

nb::dict build_context_dict(const Context& ctx) {
    const auto& cpu = ctx.cpu_info;
    const auto& sys = ctx.sys_info;
    nb::dict d;
    d["num_cpus"] = cpu.num_cpus;
    d["mhz_per_cpu"] = cpu.cycles_per_second / 1e6;
    const char* scaling = "unknown";
    if (cpu.scaling == benchmark::CPUInfo::ENABLED)
        scaling = "enabled";
    else if (cpu.scaling == benchmark::CPUInfo::DISABLED)
        scaling = "disabled";
    d["cpu_scaling"] = scaling;
    d["library_build_type"] = std::string(benchmark::GetBenchmarkVersion());
    d["host_name"] = sys.name;
    d["executable"] =
        Context::executable_name ? std::string(Context::executable_name) : std::string();
    return d;
}

class PyReporter : public BenchmarkReporter {
   public:
    nb::object py;
    // First Python exception raised by any reporter callback. Stashed (not
    // logged) so `run_benchmarks` can re-throw it after the benchmark loop
    // returns — Google Benchmark's reporter interface is noexcept, so we
    // can't let it propagate from inside the callback.
    std::exception_ptr pending_exception;

    explicit PyReporter(nb::object obj) : py(std::move(obj)) {}

    ~PyReporter() override {
        nb::gil_scoped_acquire gil;
        py.reset();
    }

    bool ReportContext(const Context& ctx) override {
        nb::gil_scoped_acquire gil;
        try {
            auto res = py.attr("report_context")(build_context_dict(ctx));
            if (res.is_none()) return true;
            return nb::cast<bool>(res);
        } catch (...) {
            if (!pending_exception) pending_exception = std::current_exception();
            return false;
        }
    }

    void ReportRuns(const std::vector<Run>& runs) override {
        nb::gil_scoped_acquire gil;
        try {
            nb::list py_runs;
            for (const auto& r : runs) {
                py_runs.append(nb::cast(r, nb::rv_policy::copy));
            }
            py.attr("report_runs")(py_runs);
        } catch (...) {
            if (!pending_exception) pending_exception = std::current_exception();
        }
    }

    void Finalize() override {
        nb::gil_scoped_acquire gil;
        try {
            if (nb::hasattr(py, "finalize")) py.attr("finalize")();
        } catch (...) {
            if (!pending_exception) pending_exception = std::current_exception();
        }
    }
};

}  // namespace

void register_reporter(nb::module_& m) {
    nb::enum_<benchmark::TimeUnit>(m, "TimeUnit")
        .value("ns", benchmark::kNanosecond)
        .value("us", benchmark::kMicrosecond)
        .value("ms", benchmark::kMillisecond)
        .value("s", benchmark::kSecond);

    nb::enum_<Run::RunType>(m, "RunType")
        .value("iteration", Run::RT_Iteration)
        .value("aggregate", Run::RT_Aggregate);

    nb::class_<benchmark::BenchmarkName>(m, "BenchmarkName")
        .def_ro("function_name", &benchmark::BenchmarkName::function_name)
        .def_ro("args", &benchmark::BenchmarkName::args)
        .def_ro("min_time", &benchmark::BenchmarkName::min_time)
        .def_ro("min_warmup_time", &benchmark::BenchmarkName::min_warmup_time)
        .def_ro("iterations", &benchmark::BenchmarkName::iterations)
        .def_ro("repetitions", &benchmark::BenchmarkName::repetitions)
        .def_ro("time_type", &benchmark::BenchmarkName::time_type)
        .def_ro("threads", &benchmark::BenchmarkName::threads)
        .def("__str__", &benchmark::BenchmarkName::str);

    nb::class_<Run>(m, "Run",
                    "A single benchmark run report. Times are in seconds (accumulated across "
                    "iterations); use `adjusted_real_time()` for per-iteration averages.")
        .def_ro("run_name", &Run::run_name)
        .def("benchmark_name", &Run::benchmark_name)
        .def_ro("family_index", &Run::family_index)
        .def_ro("per_family_instance_index", &Run::per_family_instance_index)
        .def_ro("run_type", &Run::run_type)
        .def_ro("aggregate_name", &Run::aggregate_name)
        .def_ro("report_label", &Run::report_label)
        .def_ro("skip_message", &Run::skip_message)
        .def_ro("iterations", &Run::iterations)
        .def_ro("threads", &Run::threads)
        .def_ro("repetition_index", &Run::repetition_index)
        .def_ro("repetitions", &Run::repetitions)
        .def_ro("time_unit", &Run::time_unit)
        .def_ro("real_accumulated_time", &Run::real_accumulated_time)
        .def_ro("cpu_accumulated_time", &Run::cpu_accumulated_time)
        .def("adjusted_real_time", &Run::GetAdjustedRealTime)
        .def("adjusted_cpu_time", &Run::GetAdjustedCPUTime)
        .def_ro("complexity_n", &Run::complexity_n)
        .def_prop_ro("counters",
                     [](const Run& r) {
                         nb::dict d;
                         for (const auto& kv : r.counters) {
                             d[kv.first.c_str()] = kv.second.value;
                         }
                         return d;
                     })
        .def_prop_ro("skipped",
                     [](const Run& r) { return r.skipped != benchmark::internal::NotSkipped; });

    m.def(
        "run_benchmarks",
        [](std::vector<std::string> argv, nb::object reporter) {
            // Stable mutable storage for argv strings; benchmark::Initialize
            // may rearrange or strip arguments in-place.
            std::vector<std::vector<char>> storage;
            storage.reserve(std::max<size_t>(argv.size(), 1));
            std::vector<char*> argp;
            argp.reserve(std::max<size_t>(argv.size(), 1));

            if (argv.empty()) {
                storage.emplace_back(std::vector<char>{'m', 'e', 'w', '\0'});
            } else {
                for (auto& s : argv) {
                    storage.emplace_back(s.begin(), s.end());
                    storage.back().push_back('\0');
                }
            }
            for (auto& v : storage) argp.push_back(v.data());

            int argc = static_cast<int>(argp.size());
            // Re-parse flags on every call so different argv per call (e.g.
            // distinct --benchmark_filter values across tests) actually take
            // effect. Initialize is safe to call repeatedly; the only
            // user-visible footgun is that `--help` in argv triggers exit(0),
            // which is documented Google Benchmark behavior.
            benchmark::Initialize(&argc, argp.data());

            std::unique_ptr<PyReporter> pr;
            if (!reporter.is_none()) {
                pr = std::make_unique<PyReporter>(reporter);
            }

            size_t count;
            {
                nb::gil_scoped_release release;
                count = pr ? benchmark::RunSpecifiedBenchmarks(pr.get())
                           : benchmark::RunSpecifiedBenchmarks();
            }

            // We deliberately do NOT call ClearRegisteredBenchmarks here.
            // Callers (mew.runner.run) clear *before* registering so a
            // second run doesn't double up, and atexit handles teardown on
            // interpreter shutdown so memory tools see a clean exit. This
            // also means BenchmarkHandle objects stay valid past a run, up
            // to the next clear.

            // Surface the first Python exception raised by any reporter
            // callback. Rethrowing a `python_error` captured via
            // `current_exception` works because nanobind's binding trampoline
            // catches it and restores the Python error indicator for us.
            if (pr && pr->pending_exception) {
                std::rethrow_exception(pr->pending_exception);
            }
            return count;
        },
        "argv"_a, "reporter"_a = nb::none(),
        "Initialize Google Benchmark with `argv`, run all registered benchmarks, "
        "then clear the registry. Returns the number of benchmarks run. Pass a "
        "Fanout reporter from Python to multiplex into multiple sinks.");
}

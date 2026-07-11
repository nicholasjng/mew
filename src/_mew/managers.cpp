// Manager bindings: let a plain Python object act as a Google Benchmark
// MemoryManager or ProfilerManager.
//
// Both managers are process-global in GB (`RegisterMemoryManager` stores a raw
// pointer and `nullptr` is the only way off), so they are exposed as scope
// objects rather than loose register/unregister functions: the `with` block owns
// the trampoline and the strong reference to the Python manager, and a manager
// can never leak into the next `mew.run()` in the same process.

#include <benchmark/benchmark.h>
#include <nanobind/nanobind.h>
#include <nanobind/stl/string.h>

#include <exception>
#include <memory>
#include <mutex>
#include <string>

#include "managers.h"

namespace nb = nanobind;
using namespace nb::literals;

namespace {

std::mutex g_exc_mutex;
std::exception_ptr g_pending_exception;

// GB's manager interfaces are noexcept. Stash the first failure and let the run
// wind down; `run_benchmarks` rethrows after the loop returns.
void stash_exception() {
    std::lock_guard<std::mutex> lock(g_exc_mutex);
    if (!g_pending_exception) g_pending_exception = std::current_exception();
}

// {"peak_bytes": 4096, ...} -> MemoryManager::Result. Keys the manager omits
// keep their tombstone / zero and are dropped by `Run.to_dict`.
void fill_memory_result(const nb::dict& d, benchmark::MemoryManager::Result& out) {
    if (d.contains("total_allocations"))
        out.num_allocs = nb::cast<int64_t>(d["total_allocations"]);
    if (d.contains("peak_bytes")) out.max_bytes_used = nb::cast<int64_t>(d["peak_bytes"]);
    if (d.contains("total_bytes"))
        out.total_allocated_bytes = nb::cast<int64_t>(d["total_bytes"]);
    if (d.contains("net_heap_growth"))
        out.net_heap_growth = nb::cast<int64_t>(d["net_heap_growth"]);
}

// One flat Python dict -> the two maps of ProfilerManager::Result, split by
// value type. The manager author writes {"profiler": "pyinstrument",
// "wall_time": 0.31} and never sees that C++ keeps strings and numbers apart.
void fill_profile_result(const nb::dict& d, benchmark::ProfilerManager::Result& out) {
    for (auto [k, v] : d) {
        std::string key = nb::cast<std::string>(nb::str(k));
        if (nb::isinstance<nb::str>(v)) {
            out.labels[key] = nb::cast<std::string>(v);
        } else {
            out.values[key] = nb::cast<double>(v);
        }
    }
}

class PyMemoryManager : public benchmark::MemoryManager {
   public:
    nb::object py;

    explicit PyMemoryManager(nb::object obj) : py(std::move(obj)) {}
    ~PyMemoryManager() override {
        nb::gil_scoped_acquire gil;
        py.reset();
    }

    void Start() override {
        nb::gil_scoped_acquire gil;
        try {
            py.attr("start")();
        } catch (...) {
            stash_exception();
        }
    }

    void Stop(Result& out) override {
        nb::gil_scoped_acquire gil;
        try {
            nb::object r = py.attr("stop")();
            if (!r.is_none()) fill_memory_result(nb::cast<nb::dict>(r), out);
        } catch (...) {
            stash_exception();
        }
    }
};

class PyProfilerManager : public benchmark::ProfilerManager {
   public:
    nb::object py;

    explicit PyProfilerManager(nb::object obj) : py(std::move(obj)) {}
    ~PyProfilerManager() override {
        nb::gil_scoped_acquire gil;
        py.reset();
    }

    void AfterSetupStart() override { call("after_setup_start"); }
    void BeforeTeardownStop() override { call("before_teardown_stop"); }

    // Called by `state.pause()` via mew_profiler_pause/resume. Optional on the
    // Python side: a manager that cannot suspend simply samples through, which
    // is what every out-of-process profiler does anyway.
    void Suspend() { call_optional("pause"); }
    void Resume() { call_optional("resume"); }

    void GetResult(Result& out) override {
        nb::gil_scoped_acquire gil;
        try {
            nb::object r = py.attr("get_result")();
            if (!r.is_none()) fill_profile_result(nb::cast<nb::dict>(r), out);
        } catch (...) {
            stash_exception();
        }
    }

   private:
    void call(const char* name) {
        nb::gil_scoped_acquire gil;
        try {
            py.attr(name)();
        } catch (...) {
            stash_exception();
        }
    }

    void call_optional(const char* name) {
        nb::gil_scoped_acquire gil;
        try {
            if (nb::hasattr(py, name)) py.attr(name)();
        } catch (...) {
            stash_exception();
        }
    }
};

// The registered profiler manager, so `state.pause()` can reach it. Only ever
// written under the GIL by the scope's __enter__/__exit__.
PyProfilerManager* g_profiler = nullptr;

// A registration scope. `impl` is created on __enter__ and destroyed on
// __exit__, which is also when GB is pointed back at nullptr.
template <typename Impl>
struct Scope {
    nb::object py;
    std::unique_ptr<Impl> impl;

    explicit Scope(nb::object obj) : py(std::move(obj)) {}
};

using MemoryScope = Scope<PyMemoryManager>;
using ProfilerScope = Scope<PyProfilerManager>;

}  // namespace

std::exception_ptr mew_take_pending_manager_exception() {
    std::lock_guard<std::mutex> lock(g_exc_mutex);
    std::exception_ptr p = g_pending_exception;
    g_pending_exception = nullptr;
    return p;
}

void mew_profiler_pause() {
    if (g_profiler != nullptr) g_profiler->Suspend();
}

void mew_profiler_resume() {
    if (g_profiler != nullptr) g_profiler->Resume();
}

void register_managers(nb::module_& m) {
    nb::class_<MemoryScope>(
        m, "MemoryManagerScope",
        "Context manager registering a Python memory manager with Google Benchmark.\n"
        "The object needs `start()` and `stop()`; `stop` returns a dict of the\n"
        "`memory` block's keys (peak_bytes, total_bytes, total_allocations), or None.")
        .def(
            "__enter__",
            [](MemoryScope& s) -> MemoryScope& {
                s.impl = std::make_unique<PyMemoryManager>(s.py);
                benchmark::RegisterMemoryManager(s.impl.get());
                return s;
            },
            nb::rv_policy::reference_internal, nb::sig("def __enter__(self) -> typing.Self"))
        .def(
            "__exit__",
            [](MemoryScope& s, nb::object, nb::object, nb::object) {
                benchmark::RegisterMemoryManager(nullptr);
                s.impl.reset();
            },
            "exc_type"_a.none(), "exc_value"_a.none(), "traceback"_a.none(),
            nb::sig("def __exit__(self, exc_type: type[BaseException] | None, exc_value: "
                    "BaseException | None, traceback: types.TracebackType | None) -> None"));

    nb::class_<ProfilerScope>(
        m, "ProfilerManagerScope",
        "Context manager registering a Python profiler manager with Google Benchmark.\n"
        "The object needs `after_setup_start()` and `before_teardown_stop()`, and may\n"
        "provide `get_result()` (a flat dict stamped onto the Run as `cpu_profile`)\n"
        "and `pause()`/`resume()` (called around `state.pause()` regions).")
        .def(
            "__enter__",
            [](ProfilerScope& s) -> ProfilerScope& {
                s.impl = std::make_unique<PyProfilerManager>(s.py);
                g_profiler = s.impl.get();
                benchmark::RegisterProfilerManager(s.impl.get());
                return s;
            },
            nb::rv_policy::reference_internal, nb::sig("def __enter__(self) -> typing.Self"))
        .def(
            "__exit__",
            [](ProfilerScope& s, nb::object, nb::object, nb::object) {
                benchmark::RegisterProfilerManager(nullptr);
                g_profiler = nullptr;
                s.impl.reset();
            },
            "exc_type"_a.none(), "exc_value"_a.none(), "traceback"_a.none(),
            nb::sig("def __exit__(self, exc_type: type[BaseException] | None, exc_value: "
                    "BaseException | None, traceback: types.TracebackType | None) -> None"));

    m.def(
        "memory_manager", [](nb::object obj) { return MemoryScope(std::move(obj)); }, "manager"_a,
        "Scope registering `manager` as Google Benchmark's memory manager.");
    m.def(
        "profiler_manager", [](nb::object obj) { return ProfilerScope(std::move(obj)); },
        "manager"_a, "Scope registering `manager` as Google Benchmark's profiler manager.");
}

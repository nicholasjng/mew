// Manager bindings: let a plain Python object act as a Google Benchmark
// MemoryManager or ProfilerManager.
//
// GB registration is a process-global raw pointer with `nullptr` as the only way
// off, so the trampoline lives in a static here and `mew.runner` pairs
// register/unregister on an ExitStack.

#include "managers.h"

#include <benchmark/benchmark.h>
#include <nanobind/nanobind.h>
#include <nanobind/stl/string.h>

#include <atomic>
#include <exception>
#include <memory>
#include <string>

#include "abort.h"

namespace nb = nanobind;
using namespace nb::literals;

namespace {

// Holds the Python manager and calls it under the GIL, never letting an
// exception escape into Google Benchmark.
class PyManager {
   public:
    explicit PyManager(nb::object obj) : py_(std::move(obj)) {}
    virtual ~PyManager() {
        nb::gil_scoped_acquire gil;
        py_.reset();
    }

   protected:
    // The GIL must be held by the caller: the returned object outlives this
    // frame, so a scope acquired here would release before the caller's
    // temporary is destroyed, decref'ing without the GIL. Returns None if the
    // call failed (already stashed).
    nb::object call(const char* name) {
        try {
            return py_.attr(name)();
        } catch (...) {
            mew_set_pending_abort(std::current_exception());
            return nb::none();
        }
    }

    nb::object py_;
};

// Keys the manager omits keep their tombstone and are dropped by `Run.to_dict`.
void fill_memory_result(const nb::dict& d, benchmark::MemoryManager::Result& out) {
    if (d.contains("total_allocations")) out.num_allocs = nb::cast<int64_t>(d["total_allocations"]);
    if (d.contains("peak_bytes")) out.max_bytes_used = nb::cast<int64_t>(d["peak_bytes"]);
    if (d.contains("total_bytes")) out.total_allocated_bytes = nb::cast<int64_t>(d["total_bytes"]);
    if (d.contains("net_heap_growth"))
        out.net_heap_growth = nb::cast<int64_t>(d["net_heap_growth"]);
}

// One flat Python dict -> the Result's two maps, split by value type, so the
// manager author never sees that C++ keeps strings and numbers apart.
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

class PyMemoryManager final : public benchmark::MemoryManager, public PyManager {
   public:
    using PyManager::PyManager;

    void Start() override {
        nb::gil_scoped_acquire gil;
        call("start");
    }

    void Stop(Result& out) override {
        nb::gil_scoped_acquire gil;
        nb::object r = call("stop");
        if (!r.is_none()) fill_memory_result(nb::cast<nb::dict>(r), out);
    }
};

class PyProfilerManager final : public benchmark::ProfilerManager, public PyManager {
   public:
    explicit PyProfilerManager(nb::object obj) : PyManager(std::move(obj)) {
        // Resolved once: `state.pause()` can run per iteration, so a hasattr
        // probe per call would be a permanent tax. Absent hooks mean the
        // profiler samples through the pause.
        nb::gil_scoped_acquire gil;
        if (nb::hasattr(py_, "pause")) pause_ = py_.attr("pause");
        if (nb::hasattr(py_, "resume")) resume_ = py_.attr("resume");
    }

    ~PyProfilerManager() override {
        nb::gil_scoped_acquire gil;
        pause_.reset();
        resume_.reset();
    }

    void AfterSetupStart() override {
        nb::gil_scoped_acquire gil;
        active_ = true;
        call("after_setup_start");
    }
    void BeforeTeardownStop() override {
        nb::gil_scoped_acquire gil;
        active_ = false;
        call("before_teardown_stop");
    }

    // Called around a `state.pause()` region -- from *any* run, including the
    // timed one, which GB drives with no profiler manager at all. Forwarding
    // then would be a Python call per pause that suspends nothing, and in a
    // threaded run several worker threads would race on the manager's own
    // depth counter, leaving the real sampling pass unable to suspend.
    void Pause() {
        if (active_) invoke(pause_);
    }
    void Resume() {
        if (active_) invoke(resume_);
    }

    void GetResult(Result& out) override {
        nb::gil_scoped_acquire gil;
        nb::object r = call("get_result");
        if (!r.is_none()) fill_profile_result(nb::cast<nb::dict>(r), out);
    }

   private:
    void invoke(const nb::object& fn) {
        if (!fn.is_valid()) return;
        nb::gil_scoped_acquire gil;
        try {
            fn();
        } catch (...) {
            mew_set_pending_abort(std::current_exception());
        }
    }

    // True only between AfterSetupStart and BeforeTeardownStop, i.e. inside the
    // profiler's own pass. Written on that pass's single thread (GB drives it
    // through a ThreadManager(1)), read from the timed run's worker threads.
    std::atomic<bool> active_{false};
    nb::object pause_;
    nb::object resume_;
};

// GB holds raw pointers to these for the length of the run.
std::unique_ptr<PyMemoryManager> g_memory;
std::unique_ptr<PyProfilerManager> g_profiler;

}  // namespace

void mew_profiler_pause() {
    if (g_profiler) g_profiler->Pause();
}

void mew_profiler_resume() {
    if (g_profiler) g_profiler->Resume();
}

void register_managers(nb::module_& m) {
    m.def(
        "register_memory_manager",
        [](nb::object obj) {
            g_memory = std::make_unique<PyMemoryManager>(std::move(obj));
            benchmark::RegisterMemoryManager(g_memory.get());
        },
        "manager"_a,
        "Register `manager` as Google Benchmark's memory manager.\n"
        "Needs `start()` and `stop()`; `stop` returns the `memory` block's keys\n"
        "(peak_bytes, total_bytes, total_allocations) as a dict, or None.\n"
        "Pair with `unregister_memory_manager`.");
    m.def("unregister_memory_manager", [] {
        benchmark::RegisterMemoryManager(nullptr);
        g_memory.reset();
    });

    m.def(
        "register_profiler_manager",
        [](nb::object obj) {
            g_profiler = std::make_unique<PyProfilerManager>(std::move(obj));
            benchmark::RegisterProfilerManager(g_profiler.get());
        },
        "manager"_a,
        "Register `manager` as Google Benchmark's profiler manager.\n"
        "Needs `after_setup_start()` and `before_teardown_stop()`; may add\n"
        "`get_result()` (a flat dict stamped onto the Run as `cpu_profile`) and\n"
        "`pause()`/`resume()`, called around `state.pause()` regions.\n"
        "Pair with `unregister_profiler_manager`.");
    m.def("unregister_profiler_manager", [] {
        benchmark::RegisterProfilerManager(nullptr);
        g_profiler.reset();
    });
}

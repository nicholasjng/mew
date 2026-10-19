// Python interface to benchmark::State.

#include <benchmark/benchmark.h>
#include <nanobind/nanobind.h>
#include <nanobind/stl/string.h>

#include "managers.h"

namespace nb = nanobind;
using namespace nb::literals;

namespace {
// A plain CPython type rather than a nanobind class: CPython calls its methods
// without nanobind's argument dispatch, which would otherwise count toward the
// timed part of each `with state.pause()`.
//
// A scope entered while another holds the pause does nothing, so timing resumes
// when the outer scope exits. (A nested ScopedPauseTiming in Google Benchmark
// stops the stopped timer again, counting the time since it started twice.)
// `active` is set while this scope holds the pause; re-entering it then raises,
// as a flag can't tell which of its two exits ends the pause.
struct PauseScope {
    PyObject ob_base;  // PyObject_HEAD
    benchmark::State* state;
    bool active;
};

PyTypeObject* pause_scope_type = nullptr;

PyObject* pause_scope_enter(PyObject* self, PyObject*) {
    auto* scope = reinterpret_cast<PauseScope*>(self);
    if (!scope->state->in_timed_section()) {
        PyErr_SetString(PyExc_RuntimeError,
                        "state.pause() is only valid inside the benchmark loop");
        return nullptr;
    }
    if (scope->active) {
        PyErr_SetString(PyExc_RuntimeError,
                        "this pause scope is already active and cannot be re-entered");
        return nullptr;
    }
    // Inside the loop, only a pause scope stops the timer: another one holds it.
    if (!scope->state->timer_running()) return Py_NewRef(self);
    scope->state->PauseTiming();
    // Match CPU sampling to the timed region. A no-op outside the
    // single-threaded profiler pass.
    mew_profiler_pause();
    scope->active = true;
    return Py_NewRef(self);
}

PyObject* pause_scope_exit(PyObject* self, PyObject* const*, Py_ssize_t nargs) {
    if (nargs != 3) {
        PyErr_Format(PyExc_TypeError, "__exit__() takes 3 arguments (%zd given)", nargs);
        return nullptr;
    }
    auto* scope = reinterpret_cast<PauseScope*>(self);
    // Ignore a nested scope, or an unmatched direct __exit__ call.
    if (!scope->active) Py_RETURN_NONE;
    scope->active = false;
    mew_profiler_resume();
    // As in ScopedPauseTiming: a skip inside the block ends the loop, and its
    // timer must stay stopped.
    if (scope->state->in_timed_section()) scope->state->ResumeTiming();
    Py_RETURN_NONE;
}

PyMethodDef pause_scope_methods[] = {
    {"__enter__", pause_scope_enter, METH_NOARGS, nullptr},
    {"__exit__", reinterpret_cast<PyCFunction>(reinterpret_cast<void (*)()>(pause_scope_exit)),
     METH_FASTCALL, nullptr},
    {nullptr, nullptr, 0, nullptr}};

PyType_Slot pause_scope_slots[] = {
    {Py_tp_doc, (void*)"Context manager that pauses State timing within a scope."},
    {Py_tp_methods, pause_scope_methods},
    {0, nullptr}};

// Created only by State.pause(): a scope without a State would crash on entry.
// register_state() fills in the name.
PyType_Spec pause_scope_spec = {
    nullptr, sizeof(PauseScope), 0,
    Py_TPFLAGS_DEFAULT | Py_TPFLAGS_IMMUTABLETYPE | Py_TPFLAGS_DISALLOW_INSTANTIATION,
    pause_scope_slots};

PyObject* state_pause(PyObject* self, PyObject*) {
    PyObject* obj = PyType_GenericAlloc(pause_scope_type, 0);
    if (!obj) return nullptr;
    auto* scope = reinterpret_cast<PauseScope*>(obj);
    scope->state = nb::inst_ptr<benchmark::State>(self);
    scope->active = false;
    return obj;
}

// Bound through the type slots for the same reason as PauseScope's methods.
PyMethodDef state_methods[] = {
    {"pause", state_pause, METH_NOARGS,
     "Return a context manager that pauses timing for the duration of the `with` block.\n"
     "Inside another pause it does nothing. Entering it outside the benchmark loop, or\n"
     "again while it is active, raises RuntimeError."},
    {nullptr, nullptr, 0, nullptr}};

struct BatchIter {
    benchmark::State* state;
    int64_t n;
};

// Loop steps run through the iterator type slots: CPython calls them directly,
// skipping the `__next__` lookup and nanobind's argument dispatch, which cost
// about two thirds of an empty loop iteration. NULL without an exception set
// ends the loop.
PyObject* state_iternext(PyObject* self) {
    // Py_RETURN_NONE skips the reference count where None is immortal (3.12+).
    if (nb::inst_ptr<benchmark::State>(self)->KeepRunning()) Py_RETURN_NONE;
    return nullptr;
}

PyObject* batch_iternext(PyObject* self) {
    BatchIter* it = nb::inst_ptr<BatchIter>(self);
    if (it->state->KeepRunningBatch(it->n)) return PyLong_FromLongLong(it->n);
    return nullptr;
}

PyType_Slot state_slots[] = {{Py_tp_iter, (void*)PyObject_SelfIter},
                             {Py_tp_iternext, (void*)state_iternext},
                             {Py_tp_methods, state_methods},
                             {0, nullptr}};
PyType_Slot batch_slots[] = {
    {Py_tp_iter, (void*)PyObject_SelfIter}, {Py_tp_iternext, (void*)batch_iternext}, {0, nullptr}};
}  // namespace

void register_state(nb::module_& m) {
    nb::enum_<benchmark::Counter::Flags>(m, "CounterFlags", nb::is_arithmetic(), nb::is_flag(),
                                         "Flags forwarded to `benchmark::Counter`.\n"
                                         "OR together to combine (e.g. `kIsRate | kInvert`).")
        .value("kDefaults", benchmark::Counter::kDefaults)
        .value("kIsRate", benchmark::Counter::kIsRate)
        .value("kAvgThreads", benchmark::Counter::kAvgThreads)
        .value("kAvgThreadsRate", benchmark::Counter::kAvgThreadsRate)
        .value("kIsIterationInvariant", benchmark::Counter::kIsIterationInvariant)
        .value("kIsIterationInvariantRate", benchmark::Counter::kIsIterationInvariantRate)
        .value("kAvgIterations", benchmark::Counter::kAvgIterations)
        .value("kAvgIterationsRate", benchmark::Counter::kAvgIterationsRate)
        .value("kStringifyExponent", benchmark::Counter::kStringifyExponent)
        .value("kInvert", benchmark::Counter::kInvert);

    nb::enum_<benchmark::Counter::OneK>(m, "CounterOneK",
                                        "Base used to scale a counter for display.")
        .value("kIs1000", benchmark::Counter::kIs1000)
        .value("kIs1024", benchmark::Counter::kIs1024);

    nb::class_<BatchIter>(m, "BatchIter", nb::type_slots(batch_slots),
                          "Iterator yielding batch sizes from `State.batches`.");

    // Qualified by the module's import name, as nanobind does for its classes, so
    // the stub generator (which imports `_core`) sees a type of this module.
    // Static: Python 3.11 keeps the pointer as the type's name.
    static const std::string pause_scope_name =
        nb::cast<std::string>(m.attr("__name__")) + ".PauseScope";
    pause_scope_spec.name = pause_scope_name.c_str();
    pause_scope_type = reinterpret_cast<PyTypeObject*>(PyType_FromSpec(&pause_scope_spec));
    if (!pause_scope_type) throw nb::python_error();
    m.attr("PauseScope") = nb::borrow(reinterpret_cast<PyObject*>(pause_scope_type));

    nb::class_<benchmark::State>(m, "State", nb::type_slots(state_slots),
                                 "Active microbenchmark state.\n"
                                 "Iterate with `for _ in state:` to time the body.")
        .def(
            "batches",
            [](benchmark::State& self, int64_t n) {
                if (n <= 0) throw nb::value_error("batch size must be positive");
                return BatchIter{&self, n};
            },
            "n"_a,
            "Iterate in batches of `n`, reducing dispatch overhead for fast bodies.\n"
            "The final batch may exceed the iteration budget.")
        .def("skip_with_error", &benchmark::State::SkipWithError, "msg"_a,
             "Abort this benchmark and mark it failed; the row reports as skipped.")
        .def("skip_with_message", &benchmark::State::SkipWithMessage, "msg"_a,
             "Abort this benchmark without marking it an error (e.g. an unmet "
             "precondition).")
        .def("set_label", &benchmark::State::SetLabel, "label"_a,
             "Attach a free-form label to this benchmark's reported row.")
        .def("set_iteration_time", &benchmark::State::SetIterationTime, "seconds"_a,
             "Report the elapsed time of one iteration yourself. Only honored when "
             "the benchmark sets `use_manual_time`.")
        .def("set_items_processed", &benchmark::State::SetItemsProcessed, "items"_a,
             "Record how many items the body handled, reported as items/second.")
        .def("set_bytes_processed", &benchmark::State::SetBytesProcessed, "n_bytes"_a,
             "Record how many bytes the body handled, reported as bytes/second.")
        .def(
            "set_counter",
            [](benchmark::State& self, const std::string& name, double value,
               benchmark::Counter::Flags flags, benchmark::Counter::OneK one_k) {
                self.counters[name] = benchmark::Counter(value, flags, one_k);
            },
            "name"_a, "value"_a,
            // Keep enum names in generated signatures.
            "flags"_a.sig("CounterFlags.kDefaults") = benchmark::Counter::kDefaults,
            "one_k"_a.sig("CounterOneK.kIs1000") = benchmark::Counter::kIs1000,
            "Attach a user-defined counter, surfaced in `BenchmarkResult['counters']`.\n"
            "`flags` controls normalization (and rate/exponent display); `one_k` selects\n"
            "decimal or binary prefixes when the console shows the counter.")
        .def(
            "range",
            [](const benchmark::State& self, std::size_t pos) {
                // GB's own guard is an assert, compiled out in release builds;
                // an unchecked call would read out of bounds.
                if (pos >= self.range_size()) {
                    throw nb::index_error(("range(" + std::to_string(pos) +
                                           ") out of bounds: benchmark has " +
                                           std::to_string(self.range_size()) + " range argument(s)")
                                              .c_str());
                }
                return self.range(pos);
            },
            "pos"_a = 0, "Return the range argument at `pos`.")
        .def_prop_ro("range_size", &benchmark::State::range_size,
                     "Number of range arguments available to `range`.")
        .def_prop_ro("iterations", &benchmark::State::iterations,
                     "Iterations completed so far; the total once the loop finishes.")
        .def_prop_ro("threads", &benchmark::State::threads,
                     "Threads in this run: 1 without threaded mode or in a profiling pass.")
        .def_prop_ro("thread_index", &benchmark::State::thread_index,
                     "Index of the thread owning this State, in `[0, threads)`.")
        .def_prop_ro("name", &benchmark::State::name, "The registered benchmark name.")
        .def_prop_ro("skipped", &benchmark::State::skipped,
                     "Whether this benchmark was skipped, by either skip_with_* call.")
        .def_prop_ro("error_occurred", &benchmark::State::error_occurred,
                     "Whether the skip came from `skip_with_error` rather than "
                     "`skip_with_message`.")
        .def_ro("max_iterations", &benchmark::State::max_iterations,
                "Iteration count Google Benchmark budgeted for this run.");
}

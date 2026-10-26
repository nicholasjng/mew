# Profile native code

A Python profile can show that a NumPy call or compiled extension takes most of the time, while giving little detail inside that call.
Use a system profiler to sample native stacks when that is the question you need to answer.

## Select one workload

List the cases and choose one:

```console
$ mew list --show-cases
$ mew run benchmarks/bench_sort.py -F -k bench_sorted --min-time 5s --repetitions 1
```

Replace the file and name with your own benchmark.
Use the full `name[label]` for a parametrized case and keep `-F` so brackets are matched literally.
Aim for several seconds of measured work so startup is a small part of the recording.

The external profiler observes the entire process, including imports, setup, and Google Benchmark's calibration.
Focus on stacks beneath the benchmark function when investigating the timed operation.
Use the resulting profile to locate work, then measure changes again without the external profiler.

## Record with py-spy

[py-spy](https://github.com/benfred/py-spy#can-py-spy-profile-native-extensions) supports native extension stacks on Linux and Windows on supported architectures.
Install it separately and use a version that supports your Python interpreter.
Its native mode is unavailable on macOS.

```console
$ py-spy record --native --format speedscope -o profile.json -- \
    python -m mew.cli run benchmarks/bench_sort.py -F -k bench_sorted \
    --min-time 5s --repetitions 1 --capture=no
```

Open `profile.json` in [speedscope](https://www.speedscope.app/).
Build the extension with debug symbols when you need useful native function names.
On Linux, container permissions may need to allow `ptrace`.

## Record with perf on Linux

Install the system's `perf` package and ensure performance-event permissions allow recording:

```console
$ perf record -F 1000 -g -- \
    python -m mew.cli run benchmarks/bench_sort.py -F -k bench_sorted \
    --min-time 5s --repetitions 1 --capture=no
$ perf report
```

If the stacks do not unwind correctly, check the profiler's call-graph settings and how the extension was built.

## Record with Instruments on macOS

With full Xcode installed, launch the benchmark from Instruments using the Time Profiler template.
You can also record from the command line:

```console
$ xctrace record --template "Time Profiler" --output bench.trace \
    --launch -- "$PWD/.venv/bin/python" -m mew.cli run \
    benchmarks/bench_sort.py -F -k bench_sorted \
    --min-time 5s --repetitions 1 --capture=no
$ open bench.trace
```

Adjust the interpreter path to the environment containing mew and your dependencies.
Inspect the trace in Instruments and look for the native stacks reached from the benchmark.

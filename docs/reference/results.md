# Result format

`mew run -o` writes one row per benchmark repetition.
A `.json` file holds one run as `{"benchmarks": [...]}`, and a `.jsonl` or `.jsonl.gz` file holds one row per line, so JSON and JSONL rows are the same objects.
Gzipped archives grow by one gzip member per `--append`.

```text
{
  "schema": "v1",
  "run": {"id": "01975f2e-9c40-7b31-a1d4-8f0e2c5b7a90", "date": "2026-05-19T10:00:00.000Z", "name": "before"},
  "context": {
    "mew": "0.4.0",
    "cpu_model": "Apple M1 Pro",
    "num_cpus": 10,
    "cpu_scaling_enabled": false,
    "python": {"version": "3.15.0", "implementation": "cpython", "free_threaded_build": false, "gil_enabled": true},
    "vcs": {"backend": "git", "commit": "4f1c…", "dirty": false},
    "dataset": {"name": "uniform-1k", "size": 1000}
  },
  "name": "benchmarks/bench_sort.py::bench_sort[n=1000]",
  "params": {"n": 1000},
  "repetition": 0,
  "repetitions": 10,
  "threads": 1,
  "iterations": 41213,
  "real_time": 12131.4,
  "cpu_time": 12127.9,
  "label": "n=1000",
  "skip": null,
  "counters": [{"name": "items_per_second", "value": 82432110.0, "format": {"rate": true}}]
}
```

| Field | Meaning |
| --- | --- |
| `schema` | The result format, `"v1"`. mew rejects rows of any other format, so re-run old benchmarks rather than converting their files. |
| `run` | The run's time-ordered UUIDv7 `id`, its start `date` in UTC with milliseconds, and the optional `name` from `--run-name`. |
| `context` | The mew version, machine, interpreter, profilers, and your own context. Your keys win over mew's. |
| `name` | The benchmark as mew addresses it, `file.py::func[label]`, relative to the project root, with `/threads:N` for threaded runs. Function names never contain `[`. |
| `params` | A parametrized case's arguments. |
| `real_time`, `cpu_time` | Nanoseconds per iteration. |
| `skip` | `null` for a measured row, otherwise `{"reason": "error" or "skipped", "message": ...}`. |
| `counters` | `{name, value, format}` records, with float values. |
| `memory` | With a memory profiler: `peak_bytes`, `total_allocations`, `iterations`, `allocations_per_iteration`, and optionally `total_bytes` and `net_heap_growth`. |
| `cpu_profile` | With a CPU profiler: its summary, such as `sample_count` and `top_function`. |

`context.python.free_threaded_build` records build capability and `context.python.gil_enabled` the runtime state.
A free-threaded build can have both set to `true` when an extension enabled the GIL.
`context.cpu_model` is `null` where the OS does not name the CPU.
mew does not record the host name, since it often names a person.

Context values JSON cannot hold, and parameters likewise, are stored as their type's qualified name, such as `"torch.Tensor"`.
A path is stored as its string and a tuple as a list.
Floats keep NaN and infinity as `NaN` and `Infinity`, as Python's `json` writes them.
DuckDB and Python read them back, but `jq` turns them into `null` and the largest finite double.

See {class}`mew.BenchmarkResult` for the typed definition.

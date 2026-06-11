"""cyclopts CLI: `mew run`, `mew list`."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Annotated

from cyclopts import App, Parameter
from cyclopts.help import ColumnSpec, DefaultFormatter, HelpEntry

import mew.config as _config
import mew.discovery as _discovery
from mew import (
    BENCHMARK_VERSION,
    REGISTRY,
    Entry,
    JSONLReporter,
    JSONReporter,
    ParquetReporter,
    Reporter,
    RichReporter,
    __version__ as _mew_version,
    run as _run,
)


def _short_first_name(entry: HelpEntry) -> str:
    """Render option names as ``-s, --long``, with shorts before longs."""
    parts = (
        *entry.positive_shorts,
        *entry.positive_names,
        *entry.negative_shorts,
        *entry.negative_names,
    )
    return ", ".join(parts)


def _param_columns(console, options, entries):  # noqa: ARG001
    name_column = ColumnSpec(
        renderer=_short_first_name, header="Option", justify="left", style="cyan"
    )
    description_column = ColumnSpec(renderer="description", header="Description", overflow="fold")
    return (name_column, description_column)


app = App(
    name="mew",
    help="Microbenchmarking for Python via Google Benchmark.",
    version=f"mew {_mew_version} (Google Benchmark {BENCHMARK_VERSION})",
    # Suppress auto-generated `--empty-<arg>` flags for list-typed parameters.
    default_parameter=Parameter(negative_bool=(), negative_iterable=()),
    help_formatter=DefaultFormatter(column_specs=_param_columns),
)


def _collect(
    paths: list[str],
    *,
    pattern: str | None,
    tags: list[str] | None = None,
) -> list[Entry]:
    """Resolve CLI path args into a filtered list of registered entries."""
    cfg = _config.load()
    if not paths:
        paths = list(cfg.benchpaths)

    selectors = [_discovery.parse(p) for p in paths]
    files = _discovery.collect_files(selectors, file_patterns=cfg.python_files)

    REGISTRY.clear()
    for f in files:
        _discovery.import_file(f)

    # Per-selector filter is OR'd with the global -k pattern.
    filters = [s.filter for s in selectors if s.filter]
    entries = REGISTRY.all()
    if filters:
        entries = [e for e in entries if any(f in e.name for f in filters)]
    if pattern:
        entries = [e for e in entries if pattern in e.name]
    if tags:
        wanted = set(tags)
        entries = [e for e in entries if wanted.intersection(e.tags)]
    return entries


_PATHS_HELP = (
    "Files, directories, or `<path>::<filter>` selectors to discover benchmarks from. "
    "Defaults to `[tool.mew] benchpaths`."
)


@app.command(name=["list", "ls"], usage="Usage: mew list [OPTIONS] [PATHS]")
def list_(
    paths: Annotated[list[str], Parameter(help=_PATHS_HELP)] = [],
    /,
    *,
    pattern: Annotated[
        str | None,
        Parameter(
            name=["-k", "--pattern"],
            help="List benchmarks whose name *contains* this substring (not a regex). "
            "Parametrize case labels are not part of the name and won't match.",
        ),
    ] = None,
    tag: Annotated[
        list[str],
        Parameter(
            name=["-t", "--tag"],
            help="Filter benchmarks by tag. Can be repeated, uses OR semantics.",
        ),
    ] = [],
    show_tags: Annotated[
        bool, Parameter(help="Show associated tags alongside each benchmark name.")
    ] = False,
) -> None:
    """List discovered benchmarks without running them."""
    with _discovery.discovered():
        entries = _collect(paths, pattern=pattern, tags=tag or None)
        if not entries:
            print("no benchmarks found", file=sys.stderr)
            raise SystemExit(1)
        for e in entries:
            if show_tags:
                tags_str = ",".join(sorted(e.tags)) if e.tags else "-"
                print(f"{e.name}\t[{tags_str}]")
            else:
                print(e.name)


_STDOUT_SENTINELS = frozenset({"-", "stdout"})


def _build_reporters(
    outputs: list[str],
    *,
    show_memory: bool = False,
    show_cpu: bool = False,
    show_label: bool = False,
) -> list[Reporter]:
    """Resolve ``-o`` sinks into a list of reporters.

    Sentinels ``-`` and ``stdout`` map to a rich terminal reporter; ``*.json``,
    ``*.jsonl``, and ``*.parquet`` map to file reporters.
    Defaults to a single rich reporter on stdout when no ``-o`` is provided.
    """
    if not outputs:
        return [RichReporter(show_memory=show_memory, show_cpu=show_cpu, show_label=show_label)]

    reps: list[Reporter] = []
    seen_stdout = False
    seen_files: set[Path] = set()
    for raw in outputs:
        if raw in _STDOUT_SENTINELS:
            if seen_stdout:
                print("duplicate stdout sink", file=sys.stderr)
                raise SystemExit(2)
            seen_stdout = True
            reps.append(
                RichReporter(show_memory=show_memory, show_cpu=show_cpu, show_label=show_label)
            )
            continue
        path = Path(raw).resolve()
        if path in seen_files:
            print(f"duplicate file sink: {raw}", file=sys.stderr)
            raise SystemExit(2)
        seen_files.add(path)
        suffix = path.suffix.lower()
        if suffix == ".json":
            reps.append(JSONReporter(output=Path(raw)))
        elif suffix == ".jsonl":
            reps.append(JSONLReporter(output=Path(raw)))
        elif suffix in (".parquet", ".pq"):
            reps.append(ParquetReporter(output=Path(raw)))
        else:
            print(
                f"unsupported output format: {raw} "
                "(use `-`/`stdout`, *.json, *.jsonl, *.parquet, or *.pq)",
                file=sys.stderr,
            )
            raise SystemExit(2)
    return reps


@app.command(usage="Usage: mew run [OPTIONS] [PATHS]")
def run(
    paths: Annotated[list[str], Parameter(help=_PATHS_HELP)] = [],
    /,
    *,
    pattern: Annotated[
        str | None,
        Parameter(
            name=["-k", "--pattern"],
            help="Only run benchmarks whose name *contains* this substring (not a "
            "regex). Matches the registered name (`file.py::func`); parametrize "
            "case labels like `n=10000` are not part of the name and won't match.",
        ),
    ] = None,
    tag: Annotated[
        list[str],
        Parameter(
            name=["-t", "--tag"],
            help="Filter benchmarks by tag. Can be repeated, uses OR semantics.",
        ),
    ] = [],
    output: Annotated[
        list[str],
        Parameter(
            name=["-o", "--output"],
            help="Output sink, repeatable: `-` for a rich terminal table, "
            "`<path>.{json,jsonl,parquet}` for a JSON / streaming-JSONL / Parquet "
            "file. Default: `-`.",
        ),
    ] = [],
    min_time: Annotated[
        str | None,
        Parameter(
            help="The minimum amount of time that each benchmark should run, in seconds (float, e.g. `0.5`) or number of iterations (e.g. `100x`)."
        ),
    ] = None,
    repetitions: Annotated[int | None, Parameter(help="Repeat each benchmark N times.")] = None,
    extra: Annotated[
        list[str],
        Parameter(name="--benchmark-option", help="raw arguments forwarded to Google Benchmark"),
    ] = [],
    profile_memory: Annotated[
        bool,
        Parameter(
            name="--profile-memory",
            help="Profile memory allocations with `memray` before the timing run.",
        ),
    ] = False,
    flamegraph: Annotated[
        Path | None,
        Parameter(
            name="--flamegraph",
            help="Write an HTML flame graph containing allocation data to this path. Implies `--profile-memory`.",
        ),
    ] = None,
    sample: Annotated[
        bool,
        Parameter(
            name="--sample",
            help="Sample CPU time in-process with `pyinstrument` before the timing run. "
            "Python frames only — for native/C frames use `mew profile`.",
        ),
    ] = False,
    sample_interval: Annotated[
        float,
        Parameter(
            name="--sample-interval",
            help="`pyinstrument` sampling interval in seconds (default 1e-4).",
        ),
    ] = 1e-4,
    sample_iterations: Annotated[
        int,
        Parameter(
            name="--sample-iterations",
            help="Iterations of the body per benchmark under the sampler (default 1000).",
        ),
    ] = 1000,
    sample_html: Annotated[
        Path | None,
        Parameter(
            name="--sample-html",
            help="Write a pyinstrument HTML report to this path. Implies `--sample`.",
        ),
    ] = None,
) -> None:
    """Discover and run benchmarks."""
    # discovered(): bench modules stay live for the run, cleaned up at the boundary.
    with _discovery.discovered():
        entries = _collect(paths, pattern=pattern, tags=tag or None)
        if not entries:
            print("no benchmarks found", file=sys.stderr)
            raise SystemExit(1)

        # Config defaults first so CLI flags (later) override them via gflags'
        # last-wins semantics.
        argv: list[str] = ["mew", *_config.format_benchmark_args(_config.load().benchmark_options)]
        if min_time is not None:
            argv.append(f"--benchmark_min_time={min_time}")
        if repetitions is not None:
            argv.append(f"--benchmark_repetitions={repetitions}")
        argv.extend(extra)

        reporters = _build_reporters(
            output,
            show_memory=profile_memory or flamegraph is not None,
            show_cpu=sample or sample_html is not None,
            # Label column distinguishes family case rows from the truncated name.
            show_label=any(e.case_labels for e in entries),
        )

        memory_profiles = None
        cpu_profiles = None
        if profile_memory or flamegraph is not None:
            from mew.memory import profile as _profile_mem

            memory_profiles = _profile_mem(entries, flamegraph=flamegraph)
        if sample or sample_html is not None:
            from mew.cpu import profile as _profile_cpu

            cpu_profiles = _profile_cpu(
                entries,
                output=sample_html,
                interval=sample_interval,
                inner_iterations=sample_iterations,
            )

        if memory_profiles is not None or cpu_profiles is not None:
            from mew._profile import _ProfileEnriching

            reporters = [
                _ProfileEnriching(r, memory_profiles=memory_profiles, cpu_profiles=cpu_profiles)
                for r in reporters
            ]

        _run(entries, argv=argv, reporter=reporters)


@app.command(usage="Usage: mew profile [OPTIONS] [PATHS]")
def profile(
    paths: Annotated[list[str], Parameter(help=_PATHS_HELP)] = [],
    /,
    *,
    pattern: Annotated[
        str | None,
        Parameter(
            name=["-k", "--pattern"],
            help="Only profile benchmarks whose name *contains* this substring.",
        ),
    ] = None,
    tag: Annotated[
        list[str],
        Parameter(
            name=["-t", "--tag"],
            help="Filter benchmarks by tag. Can be repeated, uses OR semantics.",
        ),
    ] = [],
    profiler: Annotated[
        str,
        Parameter(
            name=["-p", "--profiler"],
            help="Backend: `auto` (the platform's native profiler), `xctrace` "
            "(macOS), `py-spy` (Linux/Windows), or `perf` (Linux).",
        ),
    ] = "auto",
    output_dir: Annotated[
        Path,
        Parameter(
            name=["-o", "--output-dir"],
            help="Directory for the recorded artifact(s). Default: `./.mew-traces`.",
        ),
    ] = Path(".mew-traces"),
    template: Annotated[
        str,
        Parameter(
            help="(xctrace only) Instruments template name (see `xctrace list "
            "templates`) or a path to a `.tracetemplate`. Default: `Time Profiler`.",
        ),
    ] = "Time Profiler",
    iterations: Annotated[
        int,
        Parameter(
            help="Times the body runs per case under the sampler (default 100000). "
            "Out-of-process samplers run at ~1 kHz, so fast benchmarks need many reps.",
        ),
    ] = 100_000,
    time_limit: Annotated[
        str | None,
        Parameter(help="Hard cap on each recording, e.g. `10s`. Bounds a runaway body."),
    ] = None,
    separate: Annotated[
        bool,
        Parameter(
            name="--separate",
            help="(xctrace only) Write one `<case>.trace` per case instead of a "
            "single combined bundle with one run per case.",
        ),
    ] = False,
    open_app: Annotated[
        bool,
        Parameter(name="--open", help="Open the resulting artifact(s) in their viewer."),
    ] = False,
) -> None:
    """Profile benchmarks out-of-process, capturing native C frames.

    Picks a native-frame profiler (xctrace on macOS, py-spy/perf on Linux) and
    records an artifact you open in its viewer. For in-process Python-level
    sampling instead, use `mew run --sample`.
    """
    from mew import profilers

    backend = profilers.select(profiler)

    with _discovery.discovered():
        entries = _collect(paths, pattern=pattern, tags=tag or None)
        if not entries:
            print("no benchmarks found", file=sys.stderr)
            raise SystemExit(1)
        artifacts = backend.run(
            entries,
            output_dir=output_dir,
            iterations=iterations,
            time_limit=time_limit,
            template=template,
            separate=separate,
        )

    for key, path in artifacts.items():
        print(f"{key}\t{path}")
    if open_app:
        for path in dict.fromkeys(artifacts.values()):
            backend.open_artifact(path)
    if artifacts:
        print(f"Open in {backend.viewer_hint}.", file=sys.stderr)


@app.command
def compare(
    files: list[Path],
    /,
    *,
    metric: Annotated[
        str,
        Parameter(
            name=["--metric", "-m"],
            help="metric to compare: real_time, cpu_time, or iterations",
        ),
    ] = "real_time",
    pattern: Annotated[
        str | None, Parameter(name=["--pattern", "-k"], help="substring filter")
    ] = None,
    stddev: Annotated[
        bool,
        Parameter(name="--stddev", help="show stddev columns if present in the result files"),
    ] = False,
    fail_on_regression: Annotated[
        float | None,
        Parameter(
            name="--fail-on-regression",
            help="exit 2 if any benchmark is slower than baseline by more than this percent",
        ),
    ] = None,
    regressions_config: Annotated[
        Path | None,
        Parameter(
            name="--regressions-config",
            help="TOML file with [tool.mew.regressions] (default: ./pyproject.toml)",
        ),
    ] = None,
    allow: Annotated[
        list[str],
        Parameter(
            name="--allow",
            help="inline allowlist entry `PATTERN` (ignore) or `PATTERN:PCT` (per-rule threshold)",
        ),
    ] = [],
) -> None:
    """Compare benchmark result files; the first file is the baseline."""
    from mew.compare import compare as _compare

    cfg = None
    if fail_on_regression is not None or allow or regressions_config is not None:
        from mew.regressions import load_config

        cfg = load_config(
            default_threshold_pct=fail_on_regression if fail_on_regression is not None else 5.0,
            path=regressions_config,
            inline_allows=allow,
        )

    code = _compare(files, metric=metric, pattern=pattern, show_stddev=stddev, regressions=cfg)
    if code:
        raise SystemExit(code)


if __name__ == "__main__":
    app()

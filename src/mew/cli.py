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
    BENCHMARK_COMMIT as _gb_commit,
    BENCHMARK_VERSION as _gb_version,
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
    version=f"mew {_mew_version} (Google Benchmark {_gb_version}@{_gb_commit[:8]})",
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
        Parameter(name=["-k", "--pattern"], help="List all benchmarks matching the given pattern."),
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
        Parameter(name=["-k", "--pattern"], help="Only run benchmarks matching the given pattern."),
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
    profile_cpu: Annotated[
        bool,
        Parameter(
            name="--profile-cpu",
            help="Profile CPU time with `pyinstrument` before the timing run.",
        ),
    ] = False,
    cpu_interval: Annotated[
        float,
        Parameter(
            name="--cpu-interval",
            help="`pyinstrument` sampling interval in seconds (default 1e-4).",
        ),
    ] = 1e-4,
    cpu_iterations: Annotated[
        int,
        Parameter(
            name="--cpu-iterations",
            help="Iterations of the body per benchmark under the sampler (default 1000).",
        ),
    ] = 1000,
    cpu_output: Annotated[
        Path | None,
        Parameter(
            name="--cpu-output",
            help="Write a pyinstrument HTML report to this path. Implies `--profile-cpu`",
        ),
    ] = None,
) -> None:
    """Discover and run benchmarks."""
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
        show_cpu=profile_cpu or cpu_output is not None,
    )

    memory_profiles = None
    cpu_profiles = None
    if profile_memory or flamegraph is not None:
        from mew.memory import profile as _profile_mem

        memory_profiles = _profile_mem(entries, flamegraph=flamegraph)
    if profile_cpu or cpu_output is not None:
        from mew.cpu import profile as _profile_cpu

        cpu_profiles = _profile_cpu(
            entries,
            output=cpu_output,
            interval=cpu_interval,
            inner_iterations=cpu_iterations,
        )

    if memory_profiles is not None or cpu_profiles is not None:
        from mew._profile import _ProfileEnriching

        reporters = [
            _ProfileEnriching(r, memory_profiles=memory_profiles, cpu_profiles=cpu_profiles)
            for r in reporters
        ]

    _run(entries, argv=argv, reporter=reporters)


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

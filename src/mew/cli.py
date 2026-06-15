"""cyclopts CLI: `mew run`, `mew list`."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

from cyclopts import App, Parameter
from cyclopts.help import ColumnSpec, DefaultFormatter, HelpEntry

import mew.config as _config
import mew.discovery as _discovery
from mew import (
    BENCHMARK_COMMIT,
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
from mew._registry import compile_name_filter, narrow_entry

if TYPE_CHECKING:
    from mew._variants import ProfileConfig


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
    version=f"mew {_mew_version} (Google Benchmark {BENCHMARK_COMMIT[:12]} {BENCHMARK_VERSION})",
    # Suppress auto-generated `--empty-<arg>` flags for list-typed parameters.
    default_parameter=Parameter(negative_bool=(), negative_iterable=()),
    help_formatter=DefaultFormatter(column_specs=_param_columns),
)


def _collect(
    paths: list[str],
    *,
    pattern: str | None,
    tags: list[str] | None = None,
    literal: bool = False,
    stdin: bool = False,
) -> list[Entry]:
    """Resolve CLI path args into a filtered list of registered entries.

    Each selector is paired with whether its ``::filter`` is literal. Positional
    args follow ``--literal``. Stdin lines (``--stdin``) are always literal: a
    line with ``::`` is a ``path::filter`` selector (imports that path); a
    path-less line (``mew list --names-only`` output) is a name *filter* matched
    against benchmarks discovered the normal way (positional paths / benchpaths),
    so it round-trips regardless of cwd.
    """
    cfg = _config.load()
    pairs: list[tuple[_discovery.Selector, bool]] = [(_discovery.parse(p), literal) for p in paths]
    name_filters: list[str] = []
    if stdin:
        for line in sys.stdin.read().splitlines():
            line = line.strip()
            if not line:
                continue
            if "::" in line:
                pairs.append((_discovery.parse(line), True))
            else:
                name_filters.append(line)
    # Need files to import. Positional args and `::` stdin selectors supply them;
    # otherwise fall back to benchpaths — unless stdin was given but empty, where
    # an empty pipe should select nothing rather than the whole suite.
    if not pairs and (name_filters or not stdin):
        pairs = [(_discovery.parse(p), literal) for p in cfg.benchpaths]

    files = _discovery.collect_files([s for s, _ in pairs], file_patterns=cfg.python_files)

    REGISTRY.clear()
    for f in files:
        _discovery.import_file(f)

    # Per-selector filter and each path-less stdin name are OR'd together (and
    # AND'd with the global -k). Compiled up front (literal where the source says
    # so) so a bad pattern fails before any run; a partial family match narrows
    # it to the matching cases.
    try:
        selector_res = [compile_name_filter(s.filter, literal=lit) for s, lit in pairs if s.filter]
        selector_res += [compile_name_filter(n, literal=True) for n in name_filters]
        pattern_re = compile_name_filter(pattern, literal=literal) if pattern else None
    except ValueError as e:
        print(e, file=sys.stderr)
        raise SystemExit(2) from e
    entries = [
        narrowed
        for e in REGISTRY.all()
        if (narrowed := narrow_entry(e, any_of=selector_res, all_of=pattern_re)) is not None
    ]
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
            help="List benchmarks whose name matches this regex (re.search, "
            "unanchored). A plain word works as a substring; a family case also "
            "matches by its `name[label]` form. Pass `--literal` to match `[...]` "
            "without escaping.",
        ),
    ] = None,
    literal: Annotated[
        bool,
        Parameter(
            name=["-F", "--literal"],
            help="Match `-k` as a literal string, not a regex. Lets you paste a "
            "displayed `name[label]` (e.g. `bench_sort[n=1000]`) without escaping "
            "its brackets.",
        ),
    ] = False,
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
    show_cases: Annotated[
        bool,
        Parameter(
            name="--show-cases",
            help="Expand each parametrized family into one row per case "
            "(`name[label]`), matching what `mew run` would execute.",
        ),
    ] = False,
    names_only: Annotated[
        bool,
        Parameter(
            name=["-n", "--names-only"],
            help="Print the bare benchmark name without the `file.py::` prefix "
            "(like `docker ps -q`). The names are path-free, so "
            "`mew list --names-only | mew run --stdin` round-trips from any "
            "directory — `mew run` resolves them against its own discovery.",
        ),
    ] = False,
) -> None:
    """List discovered benchmarks without running them."""
    with _discovery.discovered():
        entries = _collect(paths, pattern=pattern, tags=tag or None, literal=literal)
        if not entries:
            print("no benchmarks found", file=sys.stderr)
            raise SystemExit(1)
        for e in entries:
            tags_suffix = f"\t[{','.join(sorted(e.tags)) if e.tags else '-'}]" if show_tags else ""
            # --names-only drops the `file.py::` prefix for a cwd-independent id.
            base = e.name.rsplit("::", 1)[-1] if names_only else e.name
            # Expand family cases by label: those a -k narrowed to, or all with
            # --show-cases. Matches what `mew run` executes.
            if e.case_labels is not None and (e.cases is not None or show_cases):
                indices = e.cases if e.cases is not None else range(len(e.case_labels))
                for i in indices:
                    print(f"{base}[{e.case_labels[i]}]{tags_suffix}")
            else:
                print(f"{base}{tags_suffix}")


_STDOUT_SENTINELS = frozenset({"-", "stdout"})


def _build_reporters(
    outputs: list[str],
    *,
    show_memory: bool = False,
    show_cpu: bool = False,
    show_label: bool = False,
    show_variant: bool = False,
    append: bool = False,
) -> list[Reporter]:
    """Resolve ``-o`` sinks into a list of reporters.

    ``-``/``stdout`` map to a rich terminal reporter; ``*.json``/``*.jsonl``/
    ``*.parquet`` to file reporters. Defaults to one rich reporter when no ``-o``
    is given. ``append`` adds the run as a new session to existing ``.jsonl`` /
    ``.parquet`` sinks (rejected for ``.json``, a single streamed document).
    """

    def _rich() -> RichReporter:
        return RichReporter(
            show_memory=show_memory,
            show_cpu=show_cpu,
            show_label=show_label,
            show_variant=show_variant,
        )

    if not outputs:
        return [_rich()]

    reps: list[Reporter] = []
    seen_stdout = False
    seen_files: set[Path] = set()
    for raw in outputs:
        if raw in _STDOUT_SENTINELS:
            if seen_stdout:
                print("duplicate stdout sink", file=sys.stderr)
                raise SystemExit(2)
            seen_stdout = True
            reps.append(_rich())
            continue
        path = Path(raw).resolve()
        if path in seen_files:
            print(f"duplicate file sink: {raw}", file=sys.stderr)
            raise SystemExit(2)
        seen_files.add(path)
        suffix = path.suffix.lower()
        if suffix == ".json":
            if append:
                print(
                    f"--append is not supported for the JSON sink {raw} "
                    "(a single streamed document); use *.jsonl or *.parquet",
                    file=sys.stderr,
                )
                raise SystemExit(2)
            reps.append(JSONReporter(output=Path(raw)))
        elif suffix == ".jsonl":
            reps.append(JSONLReporter(output=Path(raw), append=append))
        elif suffix in (".parquet", ".pq"):
            reps.append(ParquetReporter(output=Path(raw), append=append))
        else:
            print(
                f"unsupported output format: {raw} "
                "(use `-`/`stdout`, *.json, *.jsonl, *.parquet, or *.pq)",
                file=sys.stderr,
            )
            raise SystemExit(2)
    return reps


def _parse_variants(specs: list[str]) -> dict[str, Path]:
    """Parse repeated ``name=path`` ``--variant`` specs, erroring on malformed/dup names."""
    parsed: dict[str, Path] = {}
    for spec in specs:
        name, sep, path = spec.partition("=")
        if not sep or not name or not path:
            print(f"invalid --variant {spec!r}; expected name=path", file=sys.stderr)
            raise SystemExit(2)
        if name in parsed:
            print(f"duplicate variant name: {name!r}", file=sys.stderr)
            raise SystemExit(2)
        parsed[name] = Path(path)
    return parsed


def _load_config_and_session_tag(session_tag: str | None) -> tuple[_config.Config, str | None]:
    """Load the project config and default the session tag from git when unset.

    Default is ``git describe`` unless disabled via ``[tool.mew]
    auto_session_tag = false``. Shared by the plain and ``--variant`` run paths.
    """
    cfg = _config.load()
    if session_tag is None and cfg.auto_session_tag:
        from mew._session import derive_session_tag

        session_tag = derive_session_tag()
    return cfg, session_tag


def _run_variants_cmd(
    specs: list[str],
    *,
    output: list[str],
    pattern: str | None,
    literal: bool = False,
    tags: list[str] | None,
    min_time: str | None,
    repetitions: int | None,
    extra: list[str],
    paths: list[str],
    session_tag: str | None,
    append: bool,
    profiling: ProfileConfig | None = None,
) -> None:
    """Run the ``--variant`` path: validate, then hand off to the orchestrator."""
    from mew._variants import ProfileConfig, run_variants

    if paths:
        print("--variant and positional paths are mutually exclusive", file=sys.stderr)
        raise SystemExit(2)

    profiling = profiling or ProfileConfig()
    variants = _parse_variants(specs)
    cfg, session_tag = _load_config_and_session_tag(session_tag)

    # Repetitions become separate child invocations, so they are NOT forwarded
    # as a GB flag; min_time and raw options are.
    gb_args = _config.format_benchmark_args(cfg.benchmark_options)
    if min_time is not None:
        gb_args.append(f"--benchmark_min_time={min_time}")
    gb_args.extend(extra)

    reporters = _build_reporters(
        output,
        show_variant=True,
        show_memory=profiling.profile_memory or profiling.flamegraph is not None,
        show_cpu=profiling.sample or profiling.sample_html is not None,
        append=append,
    )

    failures = run_variants(
        variants,
        reporters=reporters,
        gb_args=gb_args,
        pattern=pattern,
        literal=literal,
        tags=tags,
        repetitions=repetitions or 1,
        session_tag=session_tag,
        profiling=profiling,
    )
    if failures:
        raise SystemExit(1)


@app.command(usage="Usage: mew run [OPTIONS] [PATHS]")
def run(
    paths: Annotated[list[str], Parameter(help=_PATHS_HELP)] = [],
    /,
    *,
    pattern: Annotated[
        str | None,
        Parameter(
            name=["-k", "--pattern"],
            help="Only run benchmarks whose name matches this regex (re.search, "
            "unanchored; a plain word works as a substring). Matches the registered "
            "name (`file.py::func`); a family case also matches by its `name[label]` "
            "form (e.g. `bench_sort[n=1000]`) — pass `--literal` to match `[...]` "
            "without escaping.",
        ),
    ] = None,
    literal: Annotated[
        bool,
        Parameter(
            name=["-F", "--literal"],
            help="Match `-k` as a literal string, not a regex. Lets you paste a "
            "displayed `name[label]` (from `mew list --show-cases`) to run one case "
            "without escaping its brackets.",
        ),
    ] = False,
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
    session_tag: Annotated[
        str | None,
        Parameter(
            name="--session-tag",
            help="Label this run's output as a session (e.g. `before`), persisted "
            "next to the generated session id. Defaults to `git describe --always "
            "--dirty` inside a checkout; disable the fallback with `[tool.mew] "
            "auto_session_tag = false`. Unrelated to `-t/--tag`, which selects "
            "which benchmarks run.",
        ),
    ] = None,
    append: Annotated[
        bool,
        Parameter(
            name="--append",
            help="Append this run as a new session to existing `.jsonl` / `.parquet` "
            "sinks instead of overwriting. Pair with `--session-tag` and select "
            "sessions later via `mew compare file@<tag>`.",
        ),
    ] = False,
    variant: Annotated[
        list[str],
        Parameter(
            name="--variant",
            help="Run a `name=path` variant in its own subprocess, repeatable. For "
            "suites that can't share an interpreter (rival engines, GIL vs "
            "free-threaded, …). Rows are tagged with the variant name; compare "
            "with `mew compare <file> --by variant`. Mutually exclusive with "
            "positional paths.",
        ),
    ] = [],
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
    memory_iterations: Annotated[
        int,
        Parameter(
            name="--memory-iterations",
            help="Measured timing-loop iterations per case under `--profile-memory` "
            "(default 100, plus a short warmup). Counting over many iterations "
            "amortizes one-time allocations, so `memory.allocations_per_iteration` "
            "is comparable across engines.",
        ),
    ] = 100,
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
    if variant:
        from mew._variants import ProfileConfig

        _run_variants_cmd(
            variant,
            output=output,
            pattern=pattern,
            literal=literal,
            tags=tag or None,
            min_time=min_time,
            repetitions=repetitions,
            extra=extra,
            paths=paths,
            session_tag=session_tag,
            append=append,
            profiling=ProfileConfig(
                profile_memory=profile_memory,
                flamegraph=flamegraph,
                memory_iterations=memory_iterations,
                sample=sample,
                sample_interval=sample_interval,
                sample_iterations=sample_iterations,
                sample_html=sample_html,
            ),
        )
        return

    # discovered(): bench modules stay live for the run, cleaned up at exit.
    with _discovery.discovered():
        entries = _collect(paths, pattern=pattern, tags=tag or None)
        if not entries:
            print("no benchmarks found", file=sys.stderr)
            raise SystemExit(1)

        cfg, session_tag = _load_config_and_session_tag(session_tag)

        # Config defaults first so later CLI flags win via gflags' last-wins rule.
        argv: list[str] = ["mew", *_config.format_benchmark_args(cfg.benchmark_options)]
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
            append=append,
        )

        memory_profiles = None
        cpu_profiles = None
        if profile_memory or flamegraph is not None:
            from mew.memory import profile as _profile_mem

            memory_profiles = _profile_mem(
                entries, flamegraph=flamegraph, iterations=memory_iterations
            )
        if sample or sample_html is not None:
            from mew.cpu import profile as _profile_cpu

            cpu_profiles = _profile_cpu(
                entries,
                output=sample_html,
                interval=sample_interval,
                inner_iterations=sample_iterations,
            )

        # `run`'s projector attaches the profiles onto each RunRow before fan-out.
        _run(
            entries,
            argv=argv,
            reporter=reporters,
            session_tag=session_tag,
            memory_profiles=memory_profiles,
            cpu_profiles=cpu_profiles,
        )


@app.command(usage="Usage: mew profile [OPTIONS] [PATHS]")
def profile(
    paths: Annotated[list[str], Parameter(help=_PATHS_HELP)] = [],
    /,
    *,
    pattern: Annotated[
        str | None,
        Parameter(
            name=["-k", "--pattern"],
            help="Only profile benchmarks whose name matches this regex (re.search).",
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
    rate: Annotated[
        int,
        Parameter(
            help="(py-spy/perf) Sampling frequency in Hz (default 1000). Ignored by xctrace.",
        ),
    ] = 1000,
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
            rate=rate,
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
            help="metric to compare: real_time, cpu_time, iterations, or (for "
            "--profile-memory results) memory.peak_bytes, memory.total_bytes, "
            "memory.total_allocations, memory.allocations_per_iteration (the "
            "cross-engine-comparable per-call allocation count)",
        ),
    ] = "real_time",
    key: Annotated[
        str | None,
        Parameter(
            name="--key",
            help="how benchmarks are matched across files: `name` (full registered "
            "name) or `func` (strip the `file.py::` prefix, for A/B suites in "
            "different files with matching function names). Defaults to `func` "
            "with `--by variant`, `name` otherwise.",
        ),
    ] = None,
    pattern: Annotated[
        str | None, Parameter(name=["--pattern", "-k"], help="regex filter (re.search)")
    ] = None,
    literal: Annotated[
        bool,
        Parameter(
            name=["-F", "--literal"],
            help="match `-k` as a literal string, not a regex (e.g. a pasted "
            "`name[label]` with brackets)",
        ),
    ] = False,
    stddev: Annotated[
        bool,
        Parameter(name="--stddev", help="show stddev columns if present in the result files"),
    ] = False,
    by: Annotated[
        str | None,
        Parameter(
            name="--by",
            help="pivot dimension: `variant` compares the variants within one "
            "`mew run --variant` result file (one column each) instead of files",
        ),
    ] = None,
    baseline: Annotated[
        str | None,
        Parameter(
            name="--baseline",
            help="with `--by variant`, the baseline variant (default: first written)",
        ),
    ] = None,
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

    code = _compare(
        files,
        metric=metric,
        key=key,
        pattern=pattern,
        literal=literal,
        show_stddev=stddev,
        by=by,
        baseline=baseline,
        regressions=cfg,
    )
    if code:
        raise SystemExit(code)


if __name__ == "__main__":
    app()

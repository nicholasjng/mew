"""argparse CLI: `mew run`, `mew list`, `mew profile`, `mew compare`.

stdlib argparse (no third-party CLI dep). Each command is a plain function with
keyword args; :func:`_build_parser` mirrors those args as ``add_argument`` calls
and :func:`main` dispatches via the parsed namespace. Help is colorized with a
small ANSI helper (:mod:`mew._console`); no third-party CLI dependency.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

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

_VERSION = f"mew {_mew_version} (Google Benchmark {BENCHMARK_COMMIT[:12]} {BENCHMARK_VERSION})"


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


def _collect_or_exit(paths: list[str], **kwargs: Any) -> list[Entry]:
    """:func:`_collect`, but exit ``1`` (the shared "nothing matched" code) if empty."""
    entries = _collect(paths, **kwargs)
    if not entries:
        print("no benchmarks found", file=sys.stderr)
        raise SystemExit(1)
    return entries


_PATHS_HELP = (
    "Files, directories, or `<path>::<filter>` selectors to discover benchmarks from. "
    "Defaults to `[tool.mew] benchpaths`."
)


def list_(
    paths: list[str],
    *,
    pattern: str | None = None,
    literal: bool = False,
    tag: list[str] | None = None,
    show_tags: bool = False,
    show_cases: bool = False,
    names_only: bool = False,
) -> None:
    """List discovered benchmarks without running them."""
    with _discovery.discovered():
        entries = _collect_or_exit(paths, pattern=pattern, tags=tag or None, literal=literal)
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
_STDOUT_FORMATS = frozenset({"rich", "json", "jsonl"})


def _build_reporters(
    outputs: list[str],
    *,
    stdout_format: str = "rich",
    show_memory: bool = False,
    show_cpu: bool = False,
    show_label: bool = False,
    show_variant: bool = False,
    append: bool = False,
) -> list[Reporter]:
    """Resolve ``-o`` sinks into a list of reporters.

    ``-``/``stdout`` map to a stdout reporter in ``stdout_format`` (``rich`` /
    ``json`` / ``jsonl``); ``*.json``/``*.jsonl``/``*.parquet`` to file reporters
    (format by extension). Defaults to one stdout reporter when no ``-o`` is
    given. ``append`` adds the run as a new session to existing ``.jsonl`` /
    ``.parquet`` sinks (rejected for ``.json``, a single streamed document).
    """

    def _stdout() -> Reporter:
        if stdout_format == "json":
            return JSONReporter(output=None)
        if stdout_format == "jsonl":
            return JSONLReporter(output=None)
        return RichReporter(
            show_memory=show_memory,
            show_cpu=show_cpu,
            show_label=show_label,
            show_variant=show_variant,
        )

    if not outputs:
        return [_stdout()]

    reps: list[Reporter] = []
    seen_stdout = False
    seen_files: set[Path] = set()
    for raw in outputs:
        if raw in _STDOUT_SENTINELS:
            if seen_stdout:
                print("duplicate stdout sink", file=sys.stderr)
                raise SystemExit(2)
            seen_stdout = True
            reps.append(_stdout())
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
    if stdout_format != "rich" and not seen_stdout:
        print(
            f"warning: --format {stdout_format} has no effect without a stdout sink "
            "(every -o target is a file); add `-o -` to also stream to stdout",
            file=sys.stderr,
        )
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
    """Load the project config and default the session tag from the VCS when unset.

    Default is the change id from the ``[tool.mew.session-tag]`` command (auto: jj, then
    git), unless disabled via ``[tool.mew.session-tag] enabled = false``.
    Shared by the plain and ``--variant`` run paths.
    """
    cfg = _config.load()
    if session_tag is None and cfg.session_tag.enabled:
        from mew._session import derive_session_tag

        session_tag = derive_session_tag(tool=cfg.session_tag.tool, args=cfg.session_tag.args)
    return cfg, session_tag


def _run_variants_cmd(
    specs: list[str],
    *,
    output: list[str],
    stdout_format: str = "rich",
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
        stdout_format=stdout_format,
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


def run(
    paths: list[str],
    *,
    pattern: str | None = None,
    literal: bool = False,
    stdin: bool = False,
    tag: list[str] | None = None,
    output: list[str] | None = None,
    format: str = "rich",
    min_time: str | None = None,
    repetitions: int | None = None,
    session_tag: str | None = None,
    append: bool = False,
    variant: list[str] | None = None,
    extra: list[str] | None = None,
    profile_memory: bool = False,
    flamegraph: Path | None = None,
    memory_iterations: int = 100,
    sample: bool = False,
    sample_interval: float = 1e-4,
    sample_iterations: int = 1000,
    sample_html: Path | None = None,
) -> None:
    """Discover and run benchmarks."""
    tag = tag or []
    output = output or []
    variant = variant or []
    extra = extra or []
    if format not in _STDOUT_FORMATS:
        print(
            f"unknown --format {format!r}; choose from {sorted(_STDOUT_FORMATS)}", file=sys.stderr
        )
        raise SystemExit(2)
    if stdin and variant:
        print("--stdin and --variant are mutually exclusive", file=sys.stderr)
        raise SystemExit(2)
    if variant:
        from mew._variants import ProfileConfig

        _run_variants_cmd(
            variant,
            output=output,
            stdout_format=format,
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
        entries = _collect_or_exit(
            paths, pattern=pattern, tags=tag or None, literal=literal, stdin=stdin
        )

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
            stdout_format=format,
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


def _quick_timing_pass(entries: list[Entry]) -> list[Any]:
    """Run a fast in-process timing pass over ``entries``; return the collected RunRows."""
    collected: list[Any] = []

    class _Collector:
        def report_context(self, context: dict[str, Any], /) -> bool:
            return True

        def report_runs(self, runs: list[Any], /) -> None:
            collected.extend(runs)

        def finalize(self) -> None:
            pass

    # Short min_time: we only need relative order, not publishable timings.
    _run(entries, argv=["mew", "--benchmark_min_time=0.05"], reporter=_Collector())
    return collected


def _select_slowest(entries: list[Entry], n: int, *, rank_from: Path | None) -> list[Entry]:
    """Keep the ``n`` slowest entries by real_time, ranked from a file or a quick pass.

    A family's time is the max over its cases (the slowest case represents its
    profiling cost). Entries with no timing rank last.
    """
    from mew.reporter import _CASE_SUFFIX_RE, _OPTION_SUFFIXES_RE

    if rank_from is not None:
        from mew.compare import _read_rows

        rows = _read_rows(rank_from)[0]
    else:
        rows = _quick_timing_pass(entries)

    # Strip GB's `/case:i` and option suffixes to recover the registered entry name.
    times: dict[str, float] = {}
    for row in rows:
        name = row.get("name")
        rt = row.get("real_time")
        if not isinstance(name, str) or rt is None or row.get("aggregate_name"):
            continue
        base = _CASE_SUFFIX_RE.sub("", _OPTION_SUFFIXES_RE.sub("", name))
        times[base] = max(times.get(base, 0.0), float(rt))

    ranked = sorted(entries, key=lambda e: times.get(e.name, 0.0), reverse=True)
    return ranked[:n]


def profile(
    paths: list[str],
    *,
    pattern: str | None = None,
    tag: list[str] | None = None,
    stdin: bool = False,
    slowest: int | None = None,
    rank_from: Path | None = None,
    profiler: str = "auto",
    output_dir: Path = Path(".mew-traces"),
    template: str = "Time Profiler",
    iterations: int = 100_000,
    time_limit: str | None = None,
    rate: int = 1000,
    separate: bool = False,
    open_app: bool = False,
) -> None:
    """Profile benchmarks out-of-process, capturing native C frames.

    Picks a native-frame profiler (xctrace on macOS, py-spy/perf on Linux) and
    records an artifact you open in its viewer. For in-process Python-level
    sampling instead, use `mew run --sample`.
    """
    from mew import profilers

    backend = profilers.select(profiler)

    with _discovery.discovered():
        entries = _collect_or_exit(paths, pattern=pattern, tags=tag or None, stdin=stdin)
        if slowest is not None:
            if slowest < 1:
                print("--slowest must be >= 1", file=sys.stderr)
                raise SystemExit(2)
            total = len(entries)
            entries = _select_slowest(entries, slowest, rank_from=rank_from)
            print(f"mew: profiling {len(entries)} slowest of {total}", file=sys.stderr)
        elif rank_from is not None:
            print("--rank-from has no effect without --slowest", file=sys.stderr)
            raise SystemExit(2)
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


def compare(
    files: list[Path],
    *,
    metric: str = "real_time",
    key: str | None = None,
    pattern: str | None = None,
    literal: bool = False,
    stddev: bool = False,
    by: str | None = None,
    baseline: str | None = None,
    fail_on_regression: float | None = None,
    regressions_config: Path | None = None,
    allow: list[str] | None = None,
) -> None:
    """Compare benchmark result files; the first file is the baseline."""
    from mew.compare import compare as _compare

    allow = allow or []
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


class _CommandHelpFormatter(argparse.HelpFormatter):
    """Help formatter for the git-style layout, with light ANSI color.

    Tweaks over the argparse default:

    * Drop the ``<command>`` metavar header argparse renders above a subparsers
      group, so commands sit directly under the ``commands:`` heading.
    * Render value placeholders as ``<spiky-braces>`` (e.g. ``--pattern
      <pattern>``) instead of ``UPPERCASE``.
    * On a color terminal, bold the section headings and tint option flags. The
      styling wraps matches *after* argparse lays the text out (ANSI codes around
      disjoint regex matches add no visible characters), so column alignment is
      untouched; it falls back to plain when stdout isn't a TTY or ``NO_COLOR``
      is set — keeping pipes, CI logs, and the docs ``--help`` capture clean.
    """

    def _format_action(self, action: argparse.Action) -> str:
        text = super()._format_action(action)
        if isinstance(action, argparse._SubParsersAction):
            _, _, text = text.partition("\n")  # strip the leading `<command>` line
        return text

    def _metavar(self, action: argparse.Action) -> str:
        return f"<{action.dest.replace('_', '-')}>"

    def _get_default_metavar_for_optional(self, action: argparse.Action) -> str:
        return self._metavar(action)

    def _get_default_metavar_for_positional(self, action: argparse.Action) -> str:
        return self._metavar(action)

    def format_help(self) -> str:
        text = super().format_help()
        # TTY check at format time, not import: the docs generator captures stdout
        # (non-TTY) and must get plain text.
        if os.environ.get("NO_COLOR") or not sys.stdout.isatty():
            return text
        from mew._console import sgr

        # Wrap disjoint matches in ANSI: column-0 headings, long/short flags, and
        # <metavars>. The patterns don't overlap (and ANSI codes carry no -, --,
        # or <>), so sequential subs never nest.
        for pattern, style in (
            (r"(?m)^[A-Za-z][A-Za-z ]*:", "bold"),
            (r"(?<![\w-])--[A-Za-z][\w-]*", "cyan"),
            (r"(?<![\w-])-[A-Za-z](?![\w-])", "green"),
            (r"<[\w-]+>", "yellow"),
        ):
            text = re.sub(pattern, lambda m, s=style: sgr(m.group(), s), text)
        return text


def completions(shell: str) -> None:
    """Print a shell-completion script for ``shell`` to stdout."""
    from mew import _completions

    sys.stdout.write(_completions.generate(shell, _build_parser()))


def _add_tag_arg(p: argparse.ArgumentParser) -> None:
    """Add the shared ``-t/--tag`` filter (identical across list/run/profile)."""
    p.add_argument(
        "-t",
        "--tag",
        action="append",
        default=[],
        help="Filter benchmarks by tag. Repeatable, OR semantics.",
    )


def _build_parser() -> argparse.ArgumentParser:
    """Build the argparse command tree. Each subparser sets ``_func`` to its handler."""
    parser = argparse.ArgumentParser(
        prog="mew",
        description="Microbenchmarking for Python via Google Benchmark.",
        formatter_class=_CommandHelpFormatter,
        # git-style: global options up front, then `<command> [<args>]` — instead
        # of argparse's default `{list,ls,run,…} ...` enumeration.
        usage="mew [-h] [--version] <command> [<args>]",
    )
    parser.add_argument("--version", action="version", version=_VERSION)
    # metavar `<command>` keeps the command list out of curly braces; prog="mew"
    # so each subcommand's own usage reads `mew run …` (not the parent's usage
    # string, which argparse would otherwise splice in).
    sub = parser.add_subparsers(dest="_command", title="commands", metavar="<command>", prog="mew")

    # mew list / ls
    p = sub.add_parser(
        "list",
        aliases=["ls"],
        help="List discovered benchmarks.",
        formatter_class=_CommandHelpFormatter,
    )
    p.add_argument("paths", nargs="*", default=[], help=_PATHS_HELP)
    p.add_argument(
        "-k",
        "--pattern",
        help="List benchmarks whose name matches this regex (re.search, unanchored). "
        "A plain word works as a substring; a family case also matches by its "
        "`name[label]` form. Pass --literal to match `[...]` without escaping.",
    )
    p.add_argument(
        "-F",
        "--literal",
        action="store_true",
        help="Match -k as a literal string, not a regex (e.g. paste `bench_sort[n=1000]`).",
    )
    _add_tag_arg(p)
    p.add_argument("--show-tags", action="store_true", help="Show tags alongside each name.")
    p.add_argument(
        "--show-cases",
        action="store_true",
        help="Expand each parametrized family into one row per case (`name[label]`).",
    )
    p.add_argument(
        "-n",
        "--names-only",
        action="store_true",
        help="Print the bare name without the `file.py::` prefix (like `docker ps -q`); "
        "path-free, so `mew list -n | mew run --stdin` round-trips from any directory.",
    )
    p.set_defaults(_func=list_)

    # mew run
    p = sub.add_parser(
        "run", help="Discover and run benchmarks.", formatter_class=_CommandHelpFormatter
    )
    p.add_argument("paths", nargs="*", default=[], help=_PATHS_HELP)
    p.add_argument(
        "-k",
        "--pattern",
        help="Only run benchmarks whose name matches this regex (re.search). A family "
        "case also matches by its `name[label]` form; pass --literal to match `[...]`.",
    )
    p.add_argument("-F", "--literal", action="store_true", help="Match -k as a literal string.")
    p.add_argument(
        "--stdin",
        action="store_true",
        help="Read newline-delimited selectors from stdin (`mew list | mew run --stdin`). "
        "Lines match literally; a path-free name is resolved against run's own discovery.",
    )
    _add_tag_arg(p)
    p.add_argument(
        "-o",
        "--output",
        action="append",
        default=[],
        help="Output sink, repeatable: `-`/`stdout` for the terminal, "
        "`<path>.{json,jsonl,parquet}` for a file. Default: `-`.",
    )
    p.add_argument(
        "--format",
        default="rich",
        metavar="(rich|json|jsonl)",
        help="Format of stdout output: `rich` (table), `json`, or `jsonl`. "
        "Use json/jsonl to pipe machine-readable rows (`mew run --format jsonl | jq`).",
    )
    p.add_argument(
        "--min-time", help="Min time per benchmark, seconds (e.g. `0.5`) or iters (`100x`)."
    )
    p.add_argument("--repetitions", type=int, metavar="<N>", help="Repeat each benchmark N times.")
    p.add_argument(
        "--session-tag",
        help="Label this run's output as a session (e.g. `before`). Defaults to "
        "the jj change id or `git describe`; disable with "
        "`[tool.mew.session-tag] enabled = false`.",
    )
    p.add_argument(
        "--append",
        action="store_true",
        help="Append as a new session to existing `.jsonl` / `.parquet` sinks.",
    )
    p.add_argument(
        "--variant",
        action="append",
        default=[],
        help="Run a `name=path` variant in its own subprocess, repeatable. Compare with "
        "`mew compare <file> --by variant`. Mutually exclusive with positional paths.",
    )
    p.add_argument(
        "--benchmark-option",
        dest="extra",
        action="append",
        default=[],
        help="Raw arguments forwarded to Google Benchmark.",
    )
    p.add_argument(
        "--profile-memory",
        action="store_true",
        help="Profile memory allocations with `memray` before the timing run.",
    )
    p.add_argument(
        "--flamegraph",
        type=Path,
        help="Write an HTML allocation flame graph to this path. Implies --profile-memory.",
    )
    p.add_argument(
        "--memory-iterations",
        type=int,
        default=100,
        help="Measured loop iterations per case under --profile-memory (default 100, + warmup).",
    )
    p.add_argument(
        "--sample",
        action="store_true",
        help="Sample CPU in-process with `pyinstrument` (Python frames; use `mew profile` for native).",
    )
    p.add_argument(
        "--sample-interval",
        type=float,
        default=1e-4,
        help="pyinstrument sampling interval in seconds (default 1e-4).",
    )
    p.add_argument(
        "--sample-iterations",
        type=int,
        metavar="<N>",
        default=1000,
        help="Iterations of the body per benchmark under the sampler (default 1000).",
    )
    p.add_argument(
        "--sample-html",
        type=Path,
        help="Write a pyinstrument HTML report to this path. Implies --sample.",
    )
    p.set_defaults(_func=run)

    # mew profile
    p = sub.add_parser(
        "profile",
        help="Profile benchmarks out-of-process (native frames).",
        formatter_class=_CommandHelpFormatter,
    )
    p.add_argument("paths", nargs="*", default=[], help=_PATHS_HELP)
    p.add_argument(
        "-k", "--pattern", help="Only profile benchmarks whose name matches this regex (re.search)."
    )
    _add_tag_arg(p)
    p.add_argument(
        "--stdin",
        action="store_true",
        help="Read newline-delimited selectors from stdin (`mew list -n | mew profile --stdin`).",
    )
    p.add_argument(
        "--slowest",
        type=int,
        metavar="<N>",
        help="Profile only the N slowest benchmarks. Ranked by --rank-from if given, "
        "else by a quick in-process timing pass.",
    )
    p.add_argument(
        "--rank-from",
        type=Path,
        help="With --slowest, rank by real_time from this result file instead of a timing pass.",
    )
    p.add_argument(
        "-p",
        "--profiler",
        default="auto",
        help="Backend: `auto`, `xctrace` (macOS), `py-spy` (Linux/Windows), or `perf` (Linux).",
    )
    p.add_argument(
        "-o",
        "--output-dir",
        type=Path,
        default=Path(".mew-traces"),
        help="Directory for the recorded artifact(s). Default: `./.mew-traces`.",
    )
    p.add_argument(
        "--template",
        default="Time Profiler",
        help="(xctrace) Instruments template name or `.tracetemplate` path. Default: `Time Profiler`.",
    )
    p.add_argument(
        "--iterations",
        type=int,
        metavar="<N>",
        default=100_000,
        help="Times the body runs per case under the sampler (default 100000).",
    )
    p.add_argument("--time-limit", help="Hard cap on each recording, e.g. `10s`.")
    p.add_argument(
        "--rate",
        type=int,
        default=1000,
        help="(py-spy/perf) Sampling frequency in Hz (default 1000). Ignored by xctrace.",
    )
    p.add_argument(
        "--separate",
        action="store_true",
        help="(xctrace) Write one `<case>.trace` per case instead of a combined bundle.",
    )
    p.add_argument(
        "--open",
        dest="open_app",
        action="store_true",
        help="Open the resulting artifact(s) in their viewer.",
    )
    p.set_defaults(_func=profile)

    # mew compare
    p = sub.add_parser(
        "compare", help="Compare benchmark result files.", formatter_class=_CommandHelpFormatter
    )
    p.add_argument("files", nargs="+", type=Path, help="Result files; the first is the baseline.")
    p.add_argument(
        "-m",
        "--metric",
        default="real_time",
        help="metric: real_time, cpu_time, iterations, or (for --profile-memory results) "
        "memory.peak_bytes / memory.total_bytes / memory.total_allocations / "
        "memory.allocations_per_iteration.",
    )
    p.add_argument(
        "--key",
        help="how benchmarks are matched: `name` (full) or `func` (strip the `file.py::` "
        "prefix). Defaults to `func` with --by variant, `name` otherwise.",
    )
    p.add_argument("-k", "--pattern", help="regex filter (re.search).")
    p.add_argument(
        "-F",
        "--literal",
        action="store_true",
        help="match -k as a literal string, not a regex.",
    )
    p.add_argument("--stddev", action="store_true", help="show stddev columns if present.")
    p.add_argument(
        "--by",
        help="pivot dimension: `variant` compares variants within one --variant file.",
    )
    p.add_argument("--baseline", help="with --by variant, the baseline variant (default: first).")
    p.add_argument(
        "--fail-on-regression",
        type=float,
        help="exit 2 if any benchmark is slower than baseline by more than this percent.",
    )
    p.add_argument(
        "--regressions-config",
        type=Path,
        help="TOML file with [tool.mew.regressions] (default: ./pyproject.toml).",
    )
    p.add_argument(
        "--allow",
        action="append",
        default=[],
        help="inline allowlist entry `PATTERN` (ignore) or `PATTERN:PCT` (per-rule threshold).",
    )
    p.set_defaults(_func=compare)

    # mew completions
    from mew._completions import SHELLS

    p = sub.add_parser(
        "completions",
        help="Print a shell-completion script for eval/install.",
        formatter_class=_CommandHelpFormatter,
    )
    p.add_argument(
        "shell",
        choices=list(SHELLS),
        metavar="<shell>",
        help=f"Target shell: {', '.join(SHELLS)}.",
    )
    p.set_defaults(_func=completions)

    return parser


def main(argv: list[str] | None = None) -> int:
    """Parse ``argv`` and dispatch to the selected command. Returns the exit code."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    func = getattr(args, "_func", None)
    if func is None:
        parser.print_help()
        return 0
    # Namespace dests mirror each command's keyword params; `_command`/`_func`
    # are internal and excluded.
    kwargs = {k: v for k, v in vars(args).items() if not k.startswith("_")}
    func(**kwargs)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

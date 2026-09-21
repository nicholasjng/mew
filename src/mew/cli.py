"""The ``mew`` command-line interface (CLI)."""

from __future__ import annotations

import argparse
import math
import os
import re
import sys
from contextlib import ExitStack
from pathlib import Path
from typing import Any

import mew.config as _config
from mew import (
    BENCHMARK_VERSION,
    REGISTRY,
    Entry,
    JSONLReporter,
    JSONReporter,
    Reporter,
    RichReporter,
    __version__ as _mew_version,
    _discovery,
    run as _run,
)
from mew._options import parse_min_time
from mew._registry import compile_name_filter

_VERSION = f"mew {_mew_version} (Google Benchmark {BENCHMARK_VERSION})"


def _load_config_or_exit() -> _config.Config:
    """Load the project config, turning a malformed ``[tool.mew]`` into a CLI error."""
    try:
        return _config.load()
    except ValueError as e:
        print(f"mew: invalid [tool.mew] config: {e}", file=sys.stderr)
        raise SystemExit(2) from e


def _benchpath_selectors(cfg: _config.Config) -> list[_discovery.Selector]:
    """Selectors for the config benchpaths, anchored at the project root.

    Config paths are declared next to pyproject.toml, so they must resolve
    against it, not the cwd, for `mew run`/`list` to work from a subdirectory.
    """
    root = cfg.project_root or Path.cwd()
    selectors: list[_discovery.Selector] = []
    for p in cfg.benchpaths:
        sel = _discovery.parse(p)
        if not sel.path.is_absolute():
            sel.path = root / sel.path
        selectors.append(sel)
    return selectors


def _import_setup(cfg: _config.Config) -> None:
    """Import ``[tool.mew] setup`` before discovery, if configured.

    Imported first and unconditionally, so what it establishes -- context
    providers, shared fixtures, environment -- applies to every invocation,
    not only the ones that happen to select the file it was written next to.
    """
    if not cfg.setup:
        return
    root = cfg.project_root or Path.cwd()
    path = Path(cfg.setup)
    if not path.is_absolute():
        path = root / path
    if not path.is_file():
        raise SystemExit(f"mew: [tool.mew] setup file not found: {path}")
    _discovery.import_file(path)


def _collect(
    paths: list[str],
    *,
    cfg: _config.Config,
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
    # Files to import come from positional args and `::` stdin selectors, else
    # benchpaths; an empty stdin pipe must select nothing, not the whole suite.
    if not pairs and (name_filters or not stdin):
        # A missing default benchpath means "nothing to discover" in a fresh
        # project, not a hard error like a mistyped positional path.
        pairs = [(s, literal) for s in _benchpath_selectors(cfg) if s.path.exists()]

    try:
        files = _discovery.collect_files([s for s, _ in pairs], file_patterns=cfg.python_files)
    except FileNotFoundError as e:
        print(f"mew: path does not exist: {e.args[0]}", file=sys.stderr)
        raise SystemExit(2) from e

    REGISTRY.clear()
    # Before the benchmark files: a provider it registers must apply to them all.
    _import_setup(cfg)
    for f in files:
        _discovery.import_file(f)

    # Keep each filter attached to its discovery path. Otherwise selecting
    # a.py::x and b.py::y also selects a.py::y and b.py::x.
    try:
        selectors = [_discovery.ResolvedSelector.resolve(s, literal=lit) for s, lit in pairs]
        names = [compile_name_filter(n, literal=True) for n in name_filters]
        pattern_re = compile_name_filter(pattern, literal=literal) if pattern else None
    except ValueError as e:
        print(e, file=sys.stderr)
        raise SystemExit(2) from e
    candidates = [
        (entry, Path(entry.file).resolve() if entry.file else None) for entry in REGISTRY.all()
    ]
    return _discovery.select_entries(
        candidates, selectors, names=names, pattern=pattern_re, tags=tags or ()
    )


def _collect_or_exit(paths: list[str], **kwargs: Any) -> list[Entry]:
    """:func:`_collect`, but exit ``1`` (the shared "nothing matched" code) if empty."""
    entries = _collect(paths, **kwargs)
    if not entries:
        print("no benchmarks found", file=sys.stderr)
        raise SystemExit(1)
    return entries


_PATHS_HELP = "Discover benchmarks from files, directories, or <path>::<filter> selectors."


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
        entries = _collect_or_exit(
            paths, cfg=_load_config_or_exit(), pattern=pattern, tags=tag or None, literal=literal
        )
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
    append: bool = False,
) -> list[Reporter]:
    """Resolve ``-o`` sinks into a list of reporters.

    ``-``/``stdout`` map to a stdout reporter in ``stdout_format`` (``rich`` /
    ``json`` / ``jsonl``); ``*.json``/``*.jsonl``/``*.jsonl.gz`` to file
    reporters (format by extension; ``.gz`` writes a gzip archive). Defaults to
    one stdout reporter when no ``-o`` is given. ``append`` adds the run as a
    new session to existing ``.jsonl[.gz]`` sinks (rejected for ``.json``, a
    single streamed document).
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
        )

    if append and not any(raw.lower().endswith((".jsonl", ".jsonl.gz")) for raw in outputs):
        print("--append requires a .jsonl or .jsonl.gz output sink", file=sys.stderr)
        raise SystemExit(2)

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
        name = path.name.lower()
        if name.endswith(".json"):
            if append:
                print(
                    f"--append is not supported for the JSON sink {raw} "
                    "(a single streamed document); use *.jsonl or *.jsonl.gz",
                    file=sys.stderr,
                )
                raise SystemExit(2)
            reps.append(JSONReporter(output=Path(raw)))
        elif name.endswith((".jsonl", ".jsonl.gz")):
            reps.append(JSONLReporter(output=Path(raw), append=append))
        else:
            print(
                f"unsupported output format: {raw} "
                "(use `-`/`stdout`, *.json, *.jsonl, or *.jsonl.gz)",
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
    min_warmup_time: float | None = None,
    random_interleaving: bool = False,
    repetitions: int | None = None,
    session_tag: str | None = None,
    append: bool = False,
    strict: bool = False,
    profile_memory: bool = False,
    flamegraph: Path | None = None,
    sample: bool = False,
    sample_interval: float = 1e-4,
    sample_html: Path | None = None,
) -> None:
    """Discover and run benchmarks."""
    tag = tag or []
    output = output or []
    if format not in _STDOUT_FORMATS:
        print(
            f"unknown --format {format!r}; choose from {sorted(_STDOUT_FORMATS)}", file=sys.stderr
        )
        raise SystemExit(2)
    cfg = _load_config_or_exit()
    # discovered(): bench modules stay live for the run, cleaned up at exit.
    with _discovery.discovered():
        entries = _collect_or_exit(
            paths, cfg=cfg, pattern=pattern, tags=tag or None, literal=literal, stdin=stdin
        )

        reporters = _build_reporters(
            output,
            stdout_format=format,
            show_memory=profile_memory or flamegraph is not None,
            show_cpu=sample or sample_html is not None,
            # Label column distinguishes family case rows from the truncated name.
            show_label=any(e.case_labels for e in entries),
            append=append,
        )

        with ExitStack() as stack:
            memory_manager = None
            profiler_manager = None
            if profile_memory or flamegraph is not None:
                from mew import memory as _memory

                memory_manager = _memory.manager(stack)
            if sample or sample_html is not None:
                from mew import cpu as _cpu

                profiler_manager = _cpu.PyinstrumentManager(interval=sample_interval)

            _run(
                entries,
                reporter=reporters,
                min_time=min_time,
                min_warmup_time=min_warmup_time,
                repetitions=repetitions,
                random_interleaving=random_interleaving,
                session_tag=session_tag,
                strict=strict,
                memory_manager=memory_manager,
                profiler_manager=profiler_manager,
            )

            # Both artifacts render from what the run already captured, so
            # neither re-executes the suite.
            if profiler_manager is not None and sample_html is not None:
                from mew import cpu as _cpu

                _cpu.write_html(profiler_manager.sessions, sample_html)
            if memory_manager is not None and flamegraph is not None:
                from mew import memory as _memory

                _memory.write_flamegraph(memory_manager, flamegraph)


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
    statistic: str | None = None,
    regression_threshold: float | None = None,
    exit_non_zero_on_regression: bool = False,
    regressions_config: Path | None = None,
) -> None:
    """Compare benchmark result files; the last file is the baseline."""
    if baseline is not None and by is None:
        print("mew compare: --baseline requires --by", file=sys.stderr)
        raise SystemExit(2)
    from mew._statistics import resolve_statistic
    from mew.compare import compare as _compare

    cfg_file = _load_config_or_exit()
    # --statistic wins; else fall back to [tool.mew] statistic; else stdlib median.
    spec = statistic if statistic is not None else cfg_file.statistic
    reduce = resolve_statistic(spec) if spec is not None else None

    # Any regression flag opts into gating, so the gate flag alone is not a
    # silent no-op; it gates at the default threshold.
    cfg = None
    if (
        regression_threshold is not None
        or regressions_config is not None
        or exit_non_zero_on_regression
    ):
        from mew.regressions import load_config

        try:
            cfg = load_config(
                default_threshold=regression_threshold if regression_threshold is not None else 5.0,
                path=regressions_config,
                root=cfg_file.project_root,
            )
        except ValueError as e:
            print(f"mew compare: invalid regressions config: {e}", file=sys.stderr)
            raise SystemExit(2) from e

    code = _compare(
        files,
        metric=metric,
        key=key,
        pattern=pattern,
        literal=literal,
        show_stddev=stddev,
        by=by,
        baseline=baseline,
        statistic=reduce,
        regressions=cfg,
    )
    # The regression panel is informational unless the caller opted into gating;
    # a `no overlap` (1) or `--by` usage error still propagates as-is.
    if code == 2 and not exit_non_zero_on_regression:
        code = 0
    if code:
        raise SystemExit(code)


class _CommandHelpFormatter(argparse.HelpFormatter):
    """Render git-style help with optional ANSI color."""

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
        # Captured help and redirected output remain plain text.
        if os.environ.get("NO_COLOR") or not sys.stdout.isatty():
            return text
        from mew._console import sgr

        # Style headings, flags, and metavariables after layout.
        for pattern, style in (
            (r"(?m)^[A-Za-z][A-Za-z ]*:", "bold"),
            (r"(?<![\w-])--[A-Za-z][\w-]*", "cyan"),
            (r"(?<![\w-])-[A-Za-z](?![\w-])", "green"),
            (r"<[^>]+>", "yellow"),
        ):
            text = re.sub(pattern, lambda m, s=style: sgr(m.group(), s), text)
        return text


def completions(shell: str) -> None:
    """Print a shell-completion script for ``shell`` to stdout."""
    from mew import _completions

    sys.stdout.write(_completions.generate(shell, _build_parser()))


def _warmup_seconds(value: str) -> float:
    """argparse type for --min-warmup-time: '0.2', '200ms', '1m' → seconds."""
    dur = value.strip()
    try:
        if dur.endswith("ms"):  # before "m": "500ms" is not minutes
            seconds = float(dur[:-2]) / 1000
        elif dur.endswith("m"):
            seconds = float(dur[:-1]) * 60
        else:
            seconds = float(dur.removesuffix("s"))
        if not math.isfinite(seconds) or seconds < 0:
            raise ValueError
        return seconds
    except ValueError:
        # ArgumentTypeError gets argparse's usage-error exit (2); SystemExit
        # would exit 1, colliding with the "nothing matched" code.
        raise argparse.ArgumentTypeError(
            f"invalid --min-warmup-time {dur!r}; use seconds ('10s', '0.5'), "
            "milliseconds ('500ms'), or minutes ('1m')"
        ) from None


def _min_time(value: str) -> str:
    """Validate seconds or an ``Nx`` fixed-iteration count for --min-time."""
    try:
        return parse_min_time(value)
    except ValueError as e:
        raise argparse.ArgumentTypeError(
            f"invalid --min-time {value!r}; use positive seconds ('0.5', '1s') "
            "or iterations ('100x')"
        ) from e


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a positive integer, got {value!r}") from None
    if parsed < 1:
        raise argparse.ArgumentTypeError(f"expected a positive integer, got {value!r}")
    return parsed


def _positive_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError:
        parsed = math.nan
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError(f"expected a positive finite number, got {value!r}")
    return parsed


def _percent(value: str) -> float:
    """argparse type for --regression-threshold: '5%' → 5.0. Requires the '%' suffix
    so the flag reads unambiguously at the call site, not just in --help."""
    try:
        if not value.endswith("%"):
            raise ValueError
        parsed = float(value[:-1])
        if not math.isfinite(parsed) or parsed < 0:
            raise ValueError
        return parsed
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a percent like '5%', got {value!r}") from None


def _add_filter_args(
    p: argparse.ArgumentParser,
    *,
    pattern_help: str,
    literal_help: str = "Treat -k as a literal string.",
) -> None:
    """Add the coupled ``-k/--pattern`` + ``-F/--literal`` pair.

    ``-F`` only means anything alongside ``-k``, so the two are always registered
    together; only the help text differs per command.
    """
    p.add_argument("-k", "--pattern", metavar="<regex>", help=pattern_help)
    p.add_argument("-F", "--literal", action="store_true", help=literal_help)


def _add_tag_arg(p: argparse.ArgumentParser) -> None:
    """Add the shared ``-t/--tag`` filter (identical across list and run)."""
    p.add_argument(
        "-t",
        "--tag",
        action="append",
        default=[],
        help="Select benchmarks with <tag> (repeatable, OR semantics).",
    )


def _add_list_cmd(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "list",
        aliases=["ls"],
        help="List discovered benchmarks.",
        formatter_class=_CommandHelpFormatter,
        add_help=False,
    )
    p.add_argument("-h", "--help", action="help", help="Show this help.")
    p.add_argument("paths", nargs="*", default=[], help=_PATHS_HELP)
    _add_filter_args(
        p,
        pattern_help="List benchmarks whose name matches <regex>.",
        literal_help="Treat -k as a literal string.",
    )
    _add_tag_arg(p)
    p.add_argument("--show-tags", action="store_true", help="Show tags alongside benchmark names.")
    p.add_argument(
        "--show-cases",
        action="store_true",
        help="Show every case in a parametrized family.",
    )
    p.add_argument(
        "-n",
        "--names-only",
        action="store_true",
        help="Omit the file.py:: prefix from benchmark names.",
    )
    p.set_defaults(_func=list_)


def _add_run_cmd(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "run",
        help="Discover and run benchmarks.",
        formatter_class=_CommandHelpFormatter,
        add_help=False,
    )
    p.add_argument("-h", "--help", action="help", help="Show this help.")
    p.add_argument("paths", nargs="*", default=[], help=_PATHS_HELP)
    _add_filter_args(
        p,
        pattern_help="Run benchmarks whose name matches <regex>.",
    )
    p.add_argument(
        "--stdin",
        action="store_true",
        help="Read literal benchmark selectors from standard input.",
    )
    _add_tag_arg(p)
    p.add_argument(
        "-o",
        "--output",
        action="append",
        default=[],
        metavar="<file>",
        help="Write results to <file> (repeatable; '-' for standard output).",
    )
    p.add_argument(
        "--format",
        default="rich",
        metavar="<format>",
        help="Set standard-output format to rich, json, or jsonl.",
    )
    p.add_argument(
        "--min-time",
        type=_min_time,
        metavar="<time>",
        help="Run each benchmark for at least <time> or N iterations.",
    )
    p.add_argument(
        "--min-warmup-time",
        type=_warmup_seconds,
        metavar="<time>",
        help="Warm up each benchmark for at least <time>.",
    )
    p.add_argument(
        "--repetitions", type=_positive_int, metavar="<n>", help="Repeat each benchmark <n> times."
    )
    p.add_argument(
        "--random-interleaving",
        action="store_true",
        help="Randomly interleave benchmark repetitions.",
    )
    p.add_argument(
        "--session-tag",
        metavar="<tag>",
        help="Identify this run as session <tag>.",
    )
    p.add_argument(
        "--append",
        action="store_true",
        help="Append a session to existing JSONL output.",
    )
    p.add_argument(
        "--strict",
        action="store_true",
        help="Fail instead of skipping unsupported threaded benchmarks.",
    )
    p.add_argument(
        "--profile-memory",
        action="store_true",
        help="Profile memory allocations with memray.",
    )
    p.add_argument(
        "--flamegraph",
        type=Path,
        metavar="<file>",
        help="Write an allocation flame graph to <file> (implies --profile-memory).",
    )
    p.add_argument(
        "--sample",
        action="store_true",
        help="Sample Python CPU usage with pyinstrument.",
    )
    p.add_argument(
        "--sample-interval",
        type=_positive_float,
        default=1e-4,
        metavar="<seconds>",
        help="Set the pyinstrument sampling interval (default 1e-4).",
    )
    p.add_argument(
        "--sample-html",
        type=Path,
        metavar="<file>",
        help="Write a pyinstrument report to <file> (implies --sample).",
    )
    p.set_defaults(_func=run)


def _add_compare_cmd(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "compare",
        help="Compare benchmark result files.",
        formatter_class=_CommandHelpFormatter,
        add_help=False,
    )
    p.add_argument("-h", "--help", action="help", help="Show this help.")
    p.add_argument(
        "files", nargs="+", type=Path, help="Compare result files against the last file."
    )
    p.add_argument(
        "-m",
        "--metric",
        default="real_time",
        help="Compare using <metric> (default real_time).",
    )
    p.add_argument(
        "--key",
        metavar="<key>",
        help="Match benchmarks by name or func (default name; func with --by).",
    )
    _add_filter_args(p, pattern_help="Compare benchmarks whose name matches <regex>.")
    p.add_argument("--stddev", action="store_true", help="Show standard-deviation columns.")
    p.add_argument(
        "--by",
        metavar="<field>",
        help="Compare groups in one file, split by <field>.",
    )
    p.add_argument("--baseline", metavar="<value>", help="Use <value> as the --by baseline.")
    p.add_argument(
        "--statistic",
        metavar="<name>",
        help="Reduce repetitions with min, max, mean, median, gmean, or pNN.",
    )
    p.add_argument(
        "--regression-threshold",
        type=_percent,
        metavar="<n%>",
        help="Report regressions over <n%%>.",
    )
    p.add_argument(
        "--exit-non-zero-on-regression",
        action="store_true",
        help="Exit with status 2 when a regression is found.",
    )
    p.add_argument(
        "--regressions-config",
        type=Path,
        metavar="<file>",
        help="Read regression rules from <file> (default pyproject.toml).",
    )
    p.set_defaults(_func=compare)


def _add_completions_cmd(sub: argparse._SubParsersAction) -> None:
    from mew._completions import SHELLS

    p = sub.add_parser(
        "completions",
        help="Print a shell-completion script for eval/install.",
        formatter_class=_CommandHelpFormatter,
        add_help=False,
    )
    p.add_argument("-h", "--help", action="help", help="Show this help.")
    p.add_argument(
        "shell",
        choices=list(SHELLS),
        metavar="<shell>",
        help=f"Generate completions for {', '.join(SHELLS)}.",
    )
    p.set_defaults(_func=completions)


def _build_parser() -> argparse.ArgumentParser:
    """Build the argparse command tree. Each subparser sets ``_func`` to its handler."""
    parser = argparse.ArgumentParser(
        prog="mew",
        description="Microbenchmarking for Python via Google Benchmark.",
        formatter_class=_CommandHelpFormatter,
        add_help=False,
        # git-style: global options up front, then `<command> [<args>]`, instead
        # of argparse's default `{list,ls,run,…} ...` enumeration.
        usage="mew [-h] [--version] <command> [<args>]",
    )
    parser.add_argument("-h", "--help", action="help", help="Show this help.")
    parser.add_argument(
        "--version", action="version", version=_VERSION, help="Show version information."
    )
    # metavar `<command>` keeps the command list out of curly braces; prog="mew"
    # so each subcommand's own usage reads `mew run …` (not the parent's usage
    # string, which argparse would otherwise splice in).
    sub = parser.add_subparsers(dest="_command", title="commands", metavar="<command>", prog="mew")

    _add_list_cmd(sub)
    _add_run_cmd(sub)
    _add_compare_cmd(sub)
    _add_completions_cmd(sub)

    return parser


def main(argv: list[str] | None = None) -> int:
    """Parse ``argv`` and dispatch to the selected command. Returns the exit code."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    func = getattr(args, "_func", None)
    if func is None:
        parser.print_help()
        return 0

    kwargs = {k: v for k, v in vars(args).items() if not k.startswith("_")}
    func(**kwargs)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

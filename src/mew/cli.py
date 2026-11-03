"""The ``mew`` command-line interface (CLI)."""

from __future__ import annotations

import argparse
import dataclasses
import math
import os
import re
import sys
import traceback
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

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
    _completions,
    _discovery,
    run as _run,
)
from mew._console import Table, Terminal, color_enabled, sgr
from mew._options import (
    check_min_warmup_time,
    check_session_tag,
    check_strict,
    parse_min_time,
    parse_seconds,
)
from mew._registry import compile_name_filter, longest_row_name, selected_cases
from mew._results import load_session, result_format, session_summaries, split_selector
from mew._statistics import resolve_statistic

_VERSION = f"mew {_mew_version} (Google Benchmark {BENCHMARK_VERSION})"


def _load_config_or_exit() -> _config.Config:
    """Load the project config, turning a malformed ``[tool.mew]`` into a CLI error."""
    try:
        return _config.load()
    except ValueError as e:
        print(f"mew: invalid config: {e}", file=sys.stderr)
        raise SystemExit(2) from e


@contextmanager
def _usage_error(command: str, *errors: type[Exception]) -> Generator[None]:
    """Turn ``errors`` into a one-line ``mew <command>: ...`` message and exit 2.

    2 is the usage-or-environment code; uncaught, they would exit 1, the code
    for "nothing matched".
    """
    try:
        yield
    except errors as e:
        print(f"mew {command}: {e}", file=sys.stderr)
        raise SystemExit(2) from e


def _import_or_exit(path: Path, command: str) -> None:
    """Import a benchmark or setup file; if it raises, exit 2 after its traceback.

    The traceback shows what the file got wrong, e.g. an option a decorator
    rejects; uncaught, it would exit 1, the code for "nothing matched".
    """
    try:
        _discovery.import_file(path)
    except Exception as e:
        traceback.print_exc()
        print(f"mew {command}: cannot import {path}", file=sys.stderr)
        raise SystemExit(2) from e


def _import_setup(cfg: _config.Config, command: str) -> None:
    """Import ``[tool.mew] setup`` before discovery, if configured.

    Imported first and unconditionally, so what it establishes -- context
    providers, shared fixtures, environment -- applies to every invocation,
    not only the ones that happen to select the file it was written next to.
    """
    if not cfg.setup:
        return
    path = (cfg.project_root or Path.cwd()) / cfg.setup  # an absolute setup replaces root
    if not path.is_file():
        print(f"mew: invalid config: [tool.mew] setup file not found: {path}", file=sys.stderr)
        raise SystemExit(2)
    _import_or_exit(path, command)


def _collect(
    paths: list[str],
    *,
    cfg: _config.Config,
    pattern: str | None,
    tags: list[str],
    literal: bool = False,
    stdin: bool = False,
    command: str,
) -> list[Entry]:
    """Resolve CLI path args into a filtered list of registered entries.

    Exits ``1`` (the shared "nothing matched" code) if nothing is selected,
    and ``2`` if a file fails to import; ``command`` names the command in
    that message.

    Stdin lines are always literal. A path-less line (``mew list --names-only``
    output) filters names over the normal discovery, so it round-trips from any cwd.
    """
    lines = [line.strip() for line in sys.stdin.read().splitlines()] if stdin else []
    names = [compile_name_filter(line, literal=True) for line in lines if line and "::" not in line]
    # Keep each filter attached to its discovery path. Otherwise selecting
    # a.py::x and b.py::y also selects a.py::y and b.py::x. Compiled before any
    # import, so a bad pattern fails before benchmark code runs.
    with _usage_error(command, ValueError):
        selectors = [_discovery.parse(p, literal=literal) for p in paths]
        selectors += [_discovery.parse(line, literal=True) for line in lines if "::" in line]
        if pattern:
            compile_name_filter(pattern, literal=literal)
    # Files to import come from positional args and `::` stdin selectors, else
    # benchpaths; an empty stdin pipe must select nothing, not the whole suite.
    if not selectors and (names or not stdin):
        # Benchpaths resolve against pyproject.toml, not the cwd, so a subdirectory
        # works. A missing one means "nothing to discover" in a fresh project, not
        # a hard error like a mistyped positional path.
        root = cfg.project_root or Path.cwd()
        selectors = [
            sel for p in cfg.benchpaths if (sel := _discovery.parse(p, root=root)).path.exists()
        ]

    try:
        files = _discovery.collect_files(selectors, file_patterns=cfg.python_files)
    except FileNotFoundError as e:
        print(f"mew: path does not exist: {e.args[0]}", file=sys.stderr)
        raise SystemExit(2) from e

    REGISTRY.clear()
    _import_setup(cfg, command)
    for f in files:
        _import_or_exit(f, command)

    candidates = [
        # abspath, not resolve(): a symlinked bench file inside a benchpath was
        # imported through the link and must stay inside that benchpath.
        (entry, Path(os.path.abspath(entry.file)) if entry.file else None)
        for entry in REGISTRY.filter(pattern, tags=tags, literal=literal)
    ]
    entries = _discovery.select_entries(candidates, selectors, names=names)
    if not entries:
        print("no benchmarks found", file=sys.stderr)
        raise SystemExit(1)
    return entries


_PATHS_HELP = "Discover benchmarks from files, directories, or <path>::<filter> selectors."


def list_(
    paths: list[str],
    *,
    pattern: str | None,
    literal: bool,
    tag: list[str],
    show_tags: bool,
    show_cases: bool,
    names_only: bool,
) -> None:
    """List discovered benchmarks without running them."""
    with _discovery.discovered():
        entries = _collect(
            paths,
            cfg=_load_config_or_exit(),
            pattern=pattern,
            tags=tag,
            literal=literal,
            command="list",
        )
        for e in entries:
            tags_suffix = f"\t[{','.join(sorted(e.tags)) if e.tags else '-'}]" if show_tags else ""
            # Expand family cases by label: those a -k narrowed to, or all with
            # --show-cases. Matches what `mew run` executes.
            if e.case_labels is not None and (e.cases is not None or show_cases):
                names = [name for _, name in selected_cases(e)]
            else:
                names = [e.name]
            for name in names:
                # --names-only drops the `file.py::` prefix for a cwd-independent id.
                print(f"{name.rsplit('::', 1)[-1] if names_only else name}{tags_suffix}")


def _build_reporters(
    outputs: list[str],
    *,
    stdout_format: str = "rich",
    show_memory: bool = False,
    show_cpu: bool = False,
    name_width: int | None = None,
    show_repetitions: bool = True,
    append: bool = False,
) -> list[Reporter]:
    """Resolve ``-o`` sinks into reporters; ``-`` is stdout, a file's format its extension.

    Raises :class:`ValueError` for a sink the run can't write to, and for
    ``append`` without a ``.jsonl`` sink or with a ``.json`` one, which is a
    single streamed document.
    """
    formats = [None if raw == "-" else result_format(raw) for raw in outputs]
    if append and ("jsonl" not in formats or "json" in formats):
        raise ValueError("--append requires a .jsonl or .jsonl.gz sink and no .json sink")

    reps: list[Reporter] = []
    seen: set[str | Path] = set()
    for raw in outputs or ["-"]:
        sink = raw if raw == "-" else Path(raw).resolve()
        if sink in seen:
            raise ValueError(f"duplicate output sink: {raw}")
        seen.add(sink)
        if isinstance(sink, str):  # stdout
            if stdout_format == "json":
                reps.append(JSONReporter(output=None))
            elif stdout_format == "jsonl":
                reps.append(JSONLReporter(output=None))
            else:
                reps.append(
                    RichReporter(
                        show_memory=show_memory,
                        show_cpu=show_cpu,
                        name_width=name_width,
                        show_repetitions=show_repetitions,
                    )
                )
            continue
        # Opened only once the run starts; fail before it does.
        if not sink.parent.is_dir():
            raise ValueError(f"output directory does not exist: {sink.parent}")
        fmt = result_format(raw)
        if fmt == "json":
            reps.append(JSONReporter(output=Path(raw)))
        elif fmt == "jsonl":
            reps.append(JSONLReporter(output=Path(raw), append=append))
        else:
            raise ValueError(
                f"unsupported output format: {raw} (use `-`, *.json, *.jsonl, or *.jsonl.gz)"
            )
    if stdout_format != "rich" and "-" not in seen:
        print(
            f"warning: --format {stdout_format} has no effect without a stdout sink "
            "(every -o target is a file); add `-o -` to also stream to stdout",
            file=sys.stderr,
        )
    return reps


def run(
    paths: list[str],
    *,
    pattern: str | None,
    literal: bool,
    stdin: bool,
    tag: list[str],
    output: list[str],
    format: str,
    min_time: str | None,
    min_warmup_time: float | None,
    random_interleaving: bool,
    repetitions: int | None,
    summary_only: bool,
    session_tag: str | None,
    append: bool,
    strict: bool,
    capture: str,
    profile_memory: bool,
    memory_iterations: int | None,
    flamegraph: Path | None,
    sample: bool,
    sample_interval: float,
    sampler: str,
    sample_html: Path | None,
) -> None:
    """Discover and run benchmarks."""
    # argparse checks the other options mew.run takes. Before discovery, so a
    # bad tag fails before any benchmark file is imported.
    if session_tag is not None:
        with _usage_error("run", TypeError, ValueError):
            check_session_tag(session_tag)
    profile_memory = profile_memory or flamegraph is not None
    sample = sample or sample_html is not None
    cfg = _load_config_or_exit()
    with _discovery.discovered():
        entries = _collect(
            paths,
            cfg=cfg,
            pattern=pattern,
            tags=tag,
            literal=literal,
            stdin=stdin,
            command="run",
        )
        if strict:
            # Here rather than in mew.run, so it exits 2 like a profiler that can't run.
            with _usage_error("run", RuntimeError):
                check_strict(entries)
        profiler_manager = None
        if sample:
            from mew import cpu as _cpu

            # A sampler that can't run here, or a bad setting for it.
            with _usage_error("run", ImportError, RuntimeError, ValueError):
                profiler_manager = _cpu.manager(interval=sample_interval, sampler=sampler)

        with _usage_error("run", ValueError):
            reporters = _build_reporters(
                output,
                stdout_format=format,
                show_memory=profile_memory,
                show_cpu=sample,
                name_width=longest_row_name(entries),
                show_repetitions=not summary_only,
                append=append,
            )

        memory_manager = None
        if profile_memory:
            from mew import memory as _memory

            with _usage_error("run", ImportError, RuntimeError):
                memory_manager = _memory.manager()

        _run(
            entries,
            reporter=reporters,
            min_time=min_time,
            min_warmup_time=min_warmup_time,
            repetitions=repetitions,
            random_interleaving=random_interleaving,
            session_tag=session_tag,
            strict=strict,
            capture_output=capture == "fd",
            memory_manager=memory_manager,
            memory_iterations=memory_iterations,
            profiler_manager=profiler_manager,
        )

        # Both artifacts render from what the run already captured, so
        # neither re-executes the suite.
        if profiler_manager is not None and sample_html is not None:
            profiler_manager.write_html(sample_html)
        if memory_manager is not None and flamegraph is not None:
            memory_manager.write_flamegraph(flamegraph)


def compare(
    files: list[Path],
    *,
    metric: str,
    key: str,
    pattern: str | None,
    literal: bool,
    stddev: bool,
    statistic: str | None,
    regression_threshold: float | None,
    exit_non_zero_on_regression: bool,
) -> None:
    """Compare benchmark result files; the last file is the baseline."""
    from mew.compare import compare as _compare

    cfg = _load_config_or_exit()
    # --statistic wins; else fall back to [tool.mew] statistic; else stdlib median.
    spec = statistic if statistic is not None else cfg.statistic
    with _usage_error("compare", ValueError):
        reduce = resolve_statistic(spec) if spec is not None else None

    # Any regression flag opts into gating, so the gate flag alone is not a
    # silent no-op; it gates at the default threshold.
    gate = None
    if regression_threshold is not None or exit_non_zero_on_regression:
        gate = cfg.regressions
        if regression_threshold is not None:  # wins over default-threshold
            gate = dataclasses.replace(gate, default_threshold=regression_threshold)

    with _usage_error("compare", ValueError, OSError):
        code = _compare(
            files,
            metric=metric,
            key=key,
            pattern=pattern,
            literal=literal,
            show_stddev=stddev,
            statistic=reduce,
            regressions=gate,
            counters=cfg.counters,
        )
    # The regression panel is informational unless the caller opted into gating;
    # a `no overlap` (1) exit still propagates as-is.
    if code == 3 and not exit_non_zero_on_regression:
        code = 0
    if code:
        raise SystemExit(code)


def show(files: list[Path], *, pattern: str | None, literal: bool, summary_only: bool) -> None:
    """Print the results stored in files as ``mew run`` printed them."""
    with _usage_error("show", ValueError):
        name_filter = compile_name_filter(pattern, literal=literal) if pattern else None
    shown = 0
    for file in files:
        path, selector = split_selector(str(file))
        with _usage_error("show", ValueError, OSError):
            rows, context = load_session(path, selector)
        if name_filter is not None:
            rows = [r for r in rows if name_filter.search(r["benchmark"])]
        if not rows:
            continue
        if len(files) > 1:
            # Which file each banner belongs to, a blank line apart.
            if shown:
                print()
            print(file)
        reporter = RichReporter(
            show_memory=any(r.get("memory") for r in rows),
            show_cpu=any(r.get("cpu_profile") for r in rows),
            name_width=max(len(r["benchmark"]) for r in rows),
            show_repetitions=not summary_only,
        )
        reporter.report_context(context)
        reporter.report_runs(rows)
        reporter.finalize()
        shown += len(rows)
    if not shown:
        print(f"mew show: no benchmarks match {pattern!r}", file=sys.stderr)
        raise SystemExit(1)


def sessions(files: list[Path]) -> None:
    """List the sessions stored in result files, newest first."""
    term = Terminal()
    for path in files:
        table = Table(title=str(path) if len(files) > 1 else None)
        table.add_column("Id")
        table.add_column("Date")
        table.add_column("Tag", flex=True)
        table.add_column("Benchmarks", justify="right")
        table.add_column("Rows", justify="right")
        with _usage_error("sessions", ValueError, OSError):
            summaries = session_summaries(path)
        for s in summaries:
            table.add_row(
                (s.id or "-")[:8],
                (s.date or "-")[:19],
                s.tag or "-",
                str(s.benchmarks),
                str(s.rows),
            )
        term.print(table)


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
        if not color_enabled(sys.stdout):
            return text

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
    sys.stdout.write(_completions.generate(shell, _build_parser()))


def _warmup_seconds(value: str) -> float:
    """Parse a ``--min-warmup-time`` value, such as ``0.2``, ``200ms`` or ``1m``, into seconds."""
    try:
        seconds = parse_seconds(value)
        check_min_warmup_time(seconds)
    except ValueError:
        # ArgumentTypeError gets argparse's usage-error exit (2); SystemExit
        # would exit 1, colliding with the "nothing matched" code.
        raise argparse.ArgumentTypeError(
            f"invalid --min-warmup-time {value!r}; use seconds ('10s', '0.5'), "
            "milliseconds ('500ms'), or minutes ('1m')"
        ) from None
    return seconds


def _min_time(value: str) -> str:
    """Validate a duration or an ``Nx`` fixed-iteration count for --min-time."""
    try:
        return parse_min_time(value)
    except ValueError as e:
        raise argparse.ArgumentTypeError(
            f"invalid --min-time {value!r}; use positive seconds ('0.5', '1s'), "
            "milliseconds ('500ms'), minutes ('1m'), or iterations ('100x')"
        ) from e


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
        if parsed < 1:
            raise ValueError
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a positive integer, got {value!r}") from None
    return parsed


def _percent(value: str) -> float:
    """Parse a ``--regression-threshold`` percent such as ``5%`` into 5.0.

    The ``%`` suffix is required, so the flag reads unambiguously on the command line.
    """
    try:
        if not value.endswith("%"):
            raise ValueError
        parsed = float(value[:-1])
        if not math.isfinite(parsed) or parsed < 0:
            raise ValueError
        return parsed
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a percent like '5%', got {value!r}") from None


def _add_filter_args(p: argparse.ArgumentParser, *, verb: str) -> None:
    """Add the coupled ``-k/--pattern`` + ``-F/--literal`` pair.

    ``-F`` only means anything alongside ``-k``, so the two are always registered
    together; only the help's verb differs per command.
    """
    p.add_argument(
        "-k", "--pattern", metavar="<regex>", help=f"{verb} benchmarks whose name matches <regex>."
    )
    p.add_argument("-F", "--literal", action="store_true", help="Treat -k as a literal string.")


def _add_summary_only_arg(p: argparse.ArgumentParser) -> None:
    """Add ``--summary-only`` (identical across run and show)."""
    p.add_argument(
        "--summary-only",
        action="store_true",
        help="Show repeated benchmarks only in the closing summary, not each repetition.",
    )


def _add_tag_arg(p: argparse.ArgumentParser) -> None:
    """Add the shared ``-t/--tag`` filter (identical across list and run)."""
    p.add_argument(
        "-t",
        "--tag",
        action="append",
        default=[],
        help="Select benchmarks with <tag> (repeatable, OR semantics).",
    )


def _add_command(
    sub: argparse._SubParsersAction, name: str, *, help: str, aliases: tuple[str, ...] = ()
) -> argparse.ArgumentParser:
    """Add a subcommand parser with mew's help formatter and ``-h/--help``."""
    p = sub.add_parser(
        name,
        aliases=aliases,
        help=help,
        formatter_class=_CommandHelpFormatter,
        add_help=False,
        allow_abbrev=False,
    )
    p.add_argument("-h", "--help", action="help", help="Show this help.")
    return p


def _add_list_cmd(sub: argparse._SubParsersAction) -> None:
    p = _add_command(sub, "list", aliases=("ls",), help="List discovered benchmarks.")
    p.add_argument("paths", nargs="*", default=[], help=_PATHS_HELP)
    _add_filter_args(p, verb="List")
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
    p = _add_command(sub, "run", help="Discover and run benchmarks.")
    p.add_argument("paths", nargs="*", default=[], help=_PATHS_HELP)
    _add_filter_args(p, verb="Run")
    p.add_argument(
        "--stdin",
        action="store_true",
        help="Read literal benchmark selectors from standard input, one per line.",
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
        choices=("rich", "json", "jsonl"),
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
    _add_summary_only_arg(p)
    p.add_argument(
        "--session-tag",
        metavar="<tag>",
        help="Identify this run as session <tag> ('latest' is reserved).",
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
        "--capture",
        choices=("fd", "no"),
        default="fd",
        help="Print benchmark stdout/stderr after the run (fd, default), or live (no).",
    )
    p.add_argument(
        "--profile-memory",
        action="store_true",
        help="Profile memory allocations with memray.",
    )
    p.add_argument(
        "--memory-iterations",
        type=_positive_int,
        metavar="<n>",
        help="Run at most <n> iterations under the memory profiler (default 16).",
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
        help="Sample Python CPU usage (see --sampler).",
    )
    p.add_argument(
        "--sampler",
        choices=("auto", "tachyon", "pyinstrument"),
        default="auto",
        help="Set the CPU sampler: tachyon (Python 3.15+), pyinstrument, or auto "
        "(default: tachyon on 3.15+, else pyinstrument).",
    )
    p.add_argument(
        "--sample-interval",
        # mew.cpu checks the range, as for the Python API.
        type=float,
        default=1e-4,
        metavar="<seconds>",
        help="Set the sampling interval (default 1e-4).",
    )
    p.add_argument(
        "--sample-html",
        type=Path,
        metavar="<file>",
        help="Write an HTML CPU profile to <file> (implies --sample).",
    )
    p.set_defaults(_func=run)


def _add_compare_cmd(sub: argparse._SubParsersAction) -> None:
    p = _add_command(sub, "compare", help="Compare benchmark result files.")
    p.add_argument(
        "files",
        nargs="+",
        type=Path,
        help="Compare result files against the last file (<file>@<tag> picks a session).",
    )
    p.add_argument(
        "-m",
        "--metric",
        default="real_time",
        help="Compare using <metric> (default real_time), or the counter counters.<name>.",
    )
    p.add_argument(
        "--key",
        default="name",
        metavar="<key>",
        help="Match benchmarks by name or func (default name).",
    )
    _add_filter_args(p, verb="Compare")
    p.add_argument("--stddev", action="store_true", help="Show standard-deviation columns.")
    p.add_argument(
        "--statistic",
        metavar="<name>",
        help="Reduce repetitions with min, max, mean, median or gmean.",
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
        help="Exit with status 3 when a regression is found.",
    )
    p.set_defaults(_func=compare)


def _add_show_cmd(sub: argparse._SubParsersAction) -> None:
    p = _add_command(sub, "show", help="Print the results stored in result files.")
    p.add_argument(
        "files",
        nargs="+",
        type=Path,
        help="Print result files' newest session (<file>@<tag> picks a session).",
    )
    _add_filter_args(p, verb="Show")
    _add_summary_only_arg(p)
    p.set_defaults(_func=show)


def _add_sessions_cmd(sub: argparse._SubParsersAction) -> None:
    p = _add_command(sub, "sessions", help="List the sessions stored in result files.")
    p.add_argument("files", nargs="+", type=Path, help="Result files to inspect.")
    p.set_defaults(_func=sessions)


def _add_completions_cmd(sub: argparse._SubParsersAction) -> None:
    p = _add_command(sub, "completions", help="Print a shell-completion script for eval/install.")
    p.add_argument(
        "shell",
        choices=_completions.SHELLS,
        metavar="<shell>",
        help=f"Generate completions for {', '.join(_completions.SHELLS)}.",
    )
    p.set_defaults(_func=completions)


def _build_parser() -> argparse.ArgumentParser:
    """Build the argparse command tree. Each subparser sets ``_func`` to its handler."""
    parser = argparse.ArgumentParser(
        prog="mew",
        description="Microbenchmarking for Python via Google Benchmark.",
        formatter_class=_CommandHelpFormatter,
        add_help=False,
        allow_abbrev=False,
        # git-style, instead of argparse's `{list,ls,run,…} ...` enumeration.
        usage="mew [-h] [--version] <command> [<args>]",
    )
    if sys.version_info >= (3, 14):
        # argparse colors help itself, under the formatter's own color. Before
        # add_subparsers, which hands the setting to each subcommand.
        parser.color = False
    parser.add_argument("-h", "--help", action="help", help="Show this help.")
    parser.add_argument(
        "--version", action="version", version=_VERSION, help="Show version information."
    )
    # prog="mew": otherwise argparse splices the parent's usage into `mew run …`.
    sub = parser.add_subparsers(dest="_command", title="commands", metavar="<command>", prog="mew")

    _add_list_cmd(sub)
    _add_run_cmd(sub)
    _add_compare_cmd(sub)
    _add_show_cmd(sub)
    _add_sessions_cmd(sub)
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

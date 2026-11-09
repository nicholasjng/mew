"""The ``mew`` command-line interface (CLI)."""

from __future__ import annotations

import argparse
import dataclasses
import math
import os
import re
import sys
import traceback
from collections.abc import Generator, Mapping
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, TextIO, cast

if TYPE_CHECKING:
    from _typeshed import SupportsWrite

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
    _managers,
    _registry,
    run as _run,
)
from mew._console import Table, Terminal, color_enabled, sgr
from mew._options import (
    check_min_warmup_time,
    check_run_name,
    check_strict,
    parse_min_time,
    parse_seconds,
)
from mew._registry import compile_name_filter, longest_row_name, selected_cases
from mew._results import load_run, resolve_statistic, result_format, run_summaries, split_selector

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
        _registry.import_file(path)
    except Exception as e:
        traceback.print_exc()
        print(f"mew {command}: cannot import {path}", file=sys.stderr)
        raise SystemExit(2) from e


def _import_setup(cfg: _config.Config, command: str) -> None:
    """Import ``[tool.mew] setup`` before discovery, if configured.

    Imported first and unconditionally, so what it establishes -- shared
    fixtures, environment, its ``context()`` hook -- applies to every
    invocation, not only the ones that happen to select the file it was
    written next to.
    """
    if not cfg.setup:
        return
    path = cfg.project_root / cfg.setup  # an absolute setup replaces root
    if not path.is_file():
        print(f"mew: invalid config: [tool.mew] setup file not found: {path}", file=sys.stderr)
        raise SystemExit(2)
    _import_or_exit(path, command)


def _setup_context(cfg: _config.Config) -> dict[str, Any]:
    """Call the setup file's ``context()``, if it defines one, for the run's context.

    Called once ``mew run`` has discovered its benchmarks, so ``mew list`` never
    runs it. A hook that raises, or returns anything but a mapping with string
    keys, exits 2 before any benchmark runs.
    """
    if not cfg.setup:
        return {}
    path = cfg.project_root / cfg.setup
    # Imported during discovery: this returns that module, without running it again.
    hook = getattr(_registry.import_file(path), "context", None)
    if not callable(hook):
        return {}
    try:
        value = hook()
    except Exception as e:
        traceback.print_exc()
        print(f"mew run: context() in {path} raised", file=sys.stderr)
        raise SystemExit(2) from e
    if not isinstance(value, Mapping) or not all(isinstance(k, str) for k in value):
        print(
            f"mew run: context() in {path} must return a dict with string keys, got {value!r}",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return dict(value)


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

    Stdin lines are selectors, as positional paths are, but always literal.
    """
    lines = [line.strip() for line in sys.stdin.read().splitlines()] if stdin else []
    # Keep each filter attached to its discovery path. Otherwise selecting
    # a.py::x and b.py::y also selects a.py::y and b.py::x. Compiled before any
    # import, so a bad pattern fails before benchmark code runs.
    with _usage_error(command, ValueError):
        selectors = [_registry.parse(p, literal=literal) for p in paths]
        selectors += [_registry.parse(line, literal=True) for line in lines if line]
        if pattern:
            compile_name_filter(pattern, literal=literal)
    # Files to import come from positional args and stdin selectors, else
    # benchpaths; an empty stdin pipe must select nothing, not the whole suite.
    if not selectors and not stdin:
        # Benchpaths resolve against pyproject.toml, not the cwd, so a subdirectory
        # works. A missing one means "nothing to discover" in a fresh project, not
        # a hard error like a mistyped positional path.
        root = cfg.project_root
        selectors = [
            sel for p in cfg.benchpaths if (sel := _registry.parse(p, root=root)).path.exists()
        ]

    try:
        files = _registry.collect_files(selectors, file_patterns=cfg.python_files)
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
    entries = _registry.select_entries(candidates, selectors)
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
) -> None:
    """List discovered benchmarks without running them."""
    with _registry.discovered():
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
                print(f"{name}{tags_suffix}")


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
    run_name: str | None,
    context_args: list[tuple[str, str]],
    append: bool,
    strict: bool,
    capture: str,
    cpu_profiler: str | None,
    memory_profiler: str | None,
    memory_iterations: int | None,
    profile_dir: Path | None,
) -> None:
    """Discover and run benchmarks."""
    # argparse checks the other options mew.run takes. Before discovery, so a
    # bad name fails before any benchmark file is imported.
    if run_name is not None:
        with _usage_error("run", TypeError, ValueError):
            check_run_name(run_name)
    if profile_dir is not None and cpu_profiler is None and memory_profiler is None:
        print("mew run: --profile-dir needs --cpu-profiler or --memory-profiler", file=sys.stderr)
        raise SystemExit(2)
    if profile_dir is not None and profile_dir.exists() and not profile_dir.is_dir():
        # Checked now, not after the suite has run.
        print(f"mew run: --profile-dir {profile_dir} is not a directory", file=sys.stderr)
        raise SystemExit(2)
    cfg = _load_config_or_exit()
    with _registry.discovered(), ExitStack() as stack:
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
        # A bad preset, or a manager that can't run here, such as a sampler
        # without its package or a bad setting for it.
        with _usage_error("run", ImportError, RuntimeError, ValueError):
            managers = _managers.load_all(
                {"cpu": cpu_profiler, "memory": memory_profiler},
                cfg.profilers,
                root=cfg.project_root,
                stack=stack,
            )
        profiler_manager = managers["cpu"].manager if "cpu" in managers else None
        memory_manager = managers["memory"].manager if "memory" in managers else None
        # --context wins over the setup file's context(), as the more specific source.
        context = {**_setup_context(cfg), **dict(context_args)}
        if managers:
            # Provenance, as the machine's: `mew compare` shows where files differ.
            context["profilers"] = {slot: m.record for slot, m in managers.items()}

        with _usage_error("run", ValueError):
            reporters = _build_reporters(
                output,
                stdout_format=format,
                show_memory=memory_manager is not None,
                show_cpu=profiler_manager is not None,
                name_width=longest_row_name(entries),
                show_repetitions=not summary_only,
                append=append,
            )

        _run(
            entries,
            reporter=reporters,
            min_time=min_time,
            min_warmup_time=min_warmup_time,
            repetitions=repetitions,
            random_interleaving=random_interleaving,
            run_name=run_name,
            context=context,
            strict=strict,
            capture_output=capture == "fd",
            memory_manager=memory_manager,
            memory_iterations=memory_iterations,
            profiler_manager=profiler_manager,
        )

        # The outputs render from what the run already recorded, so none
        # re-executes the suite.
        if profile_dir is not None:
            # A directory that can't be written, e.g. read-only, still ends in one line.
            with _usage_error("run", OSError):
                for slot, m in managers.items():
                    # Optional, as the run's own hooks are: a manager needn't subclass the protocol.
                    if (write := getattr(m.manager, "write_outputs", None)) is not None:
                        write(profile_dir / slot)


def compare(
    files: list[str],
    *,
    metric: str,
    key: str,
    pattern: str | None,
    literal: bool,
    statistic: str | None,
    regression_threshold: float | None,
    exit_non_zero_on_regression: bool,
) -> None:
    """Compare benchmark result files; the first file is the baseline."""
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


def show(files: list[str], *, pattern: str | None, literal: bool, summary_only: bool) -> None:
    """Print the results stored in files as ``mew run`` printed them."""
    with _usage_error("show", ValueError):
        name_filter = compile_name_filter(pattern, literal=literal) if pattern else None
    shown = 0
    for file in files:
        path, selector = split_selector(file)
        with _usage_error("show", ValueError, OSError):
            rows, context = load_run(path, selector)
        if name_filter is not None:
            rows = [r for r in rows if name_filter.search(r["name"])]
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
            name_width=max(len(r["name"]) for r in rows),
            show_repetitions=not summary_only,
        )
        reporter.report_context(context)
        reporter.report_results(rows)
        reporter.finalize()
        shown += len(rows)
    if not shown:
        print(f"mew show: no benchmarks match {pattern!r}", file=sys.stderr)
        raise SystemExit(1)


def _unique_prefix_width(ids: list[str]) -> int:
    """The shortest prefix, of at least 8 characters, that tells ``ids`` apart.

    A UUIDv7 leads with its time, so runs from the same minute share 8 digits.
    """
    width, distinct = 8, len(set(ids))
    longest = max(map(len, ids), default=0)
    # Past a UUID's hyphen too, which tells nothing apart.
    while width < longest and (
        len({i[:width] for i in ids}) < distinct or ids[0][width - 1 : width] == "-"
    ):
        width += 1
    return width


def log(files: list[Path]) -> None:
    """List the runs stored in result files, newest first."""
    term = Terminal()
    for path in files:
        table = Table(title=str(path) if len(files) > 1 else None)
        table.add_column("Id")
        table.add_column("Date")
        table.add_column("Name", flex=True)
        table.add_column("Benchmarks", justify="right")
        table.add_column("Rows", justify="right")
        with _usage_error("log", ValueError, OSError):
            summaries = run_summaries(path)
        width = _unique_prefix_width([s.id for s in summaries if s.id])
        for s in summaries:
            table.add_row(
                (s.id or "-")[:width],
                (s.date or "-")[:19],
                s.name or "-",
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


def _style_help(text: str) -> str:
    """Style headings, flags, and metavariables in laid-out help."""
    for pattern, style in (
        (r"(?m)^[A-Za-z][A-Za-z ]*:", "bold"),
        (r"(?<![\w-])--[A-Za-z][\w-]*", "cyan"),
        (r"(?<![\w-])-[A-Za-z](?![\w-])", "green"),
        (r"<[^>]+>", "yellow"),
    ):
        text = re.sub(pattern, lambda m, s=style: sgr(m.group(), s), text)
    return text


class _Parser(argparse.ArgumentParser):
    """Colors help and usage by the stream they go to: usage errors go to stderr.

    Subcommand parsers are this class too, as argparse makes them the parent's type.
    """

    def print_help(self, file: SupportsWrite[str] | None = None) -> None:
        self._print_styled(self.format_help(), file)

    def print_usage(self, file: SupportsWrite[str] | None = None) -> None:
        self._print_styled(self.format_usage(), file)

    def _print_styled(self, text: str, file: SupportsWrite[str] | None) -> None:
        file = sys.stdout if file is None else file
        # Captured help and redirected output remain plain text. color_enabled
        # copes with a stream that has no isatty().
        self._print_message(_style_help(text) if color_enabled(cast(TextIO, file)) else text, file)


def completions(shell: str) -> None:
    """Print a shell-completion script for ``shell`` to stdout."""
    sys.stdout.write(_completions.generate(shell, _build_parser()))


def _context_arg(value: str) -> tuple[str, str]:
    """Parse a ``--context`` value: ``key=value``, split at the first ``=``.

    The value stays a string, whatever it looks like, and the key is used as
    written: a dot doesn't nest it, as a Python dict's key wouldn't.
    """
    key, sep, text = value.partition("=")
    if not sep or not key.strip():
        raise argparse.ArgumentTypeError(f"expected <key>=<value>, got {value!r}")
    return key, text


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
        "--run-name",
        metavar="<name>",
        help="Name this run <name>, which <file>@<name> selects ('latest' and '*' are reserved).",
    )
    p.add_argument(
        "--context",
        dest="context_args",
        action="append",
        default=[],
        type=_context_arg,
        metavar="<key>=<value>",
        help="Record <key>=<value> in the run's context, over the setup file's context() "
        "(repeatable; the value is a string).",
    )
    p.add_argument(
        "--append",
        action="store_true",
        help="Append this run to existing JSONL output.",
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
        "--cpu-profiler",
        metavar="<preset>",
        help="Profile CPU time in an extra, untimed pass with <preset> from "
        "[tool.mew.profilers], the built-in 'sample', or a module:Class spec.",
    )
    p.add_argument(
        "--memory-profiler",
        metavar="<preset>",
        help="Profile memory in an extra, untimed pass with <preset> from "
        "[tool.mew.profilers], the built-in 'memray', or a module:Class spec.",
    )
    p.add_argument(
        "--memory-iterations",
        type=_positive_int,
        metavar="<n>",
        help="Run at most <n> iterations under the memory profiler (default 16).",
    )
    p.add_argument(
        "--profile-dir",
        type=Path,
        metavar="<dir>",
        help="Write the profilers' reports to <dir>/cpu and <dir>/memory.",
    )
    p.set_defaults(_func=run)


def _add_compare_cmd(sub: argparse._SubParsersAction) -> None:
    p = _add_command(sub, "compare", help="Compare benchmark result files.")
    p.add_argument(
        "files",
        nargs="+",
        help="Compare result files against the first, the baseline (<file>@<name> picks a run).",
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
        choices=("name", "func"),
        metavar="<key>",
        help="Match benchmarks by name or func (default name).",
    )
    _add_filter_args(p, verb="Compare")
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
        help="Print result files' newest run (<file>@<name> picks a run).",
    )
    _add_filter_args(p, verb="Show")
    _add_summary_only_arg(p)
    p.set_defaults(_func=show)


def _add_log_cmd(sub: argparse._SubParsersAction) -> None:
    p = _add_command(sub, "log", help="List the runs stored in result files.")
    p.add_argument("files", nargs="+", type=Path, help="Result files to inspect.")
    p.set_defaults(_func=log)


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
    parser = _Parser(
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
    _add_log_cmd(sub)
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

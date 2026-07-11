"""argparse CLI: `mew run`, `mew list`, `mew compare`.

Built on stdlib argparse, with help colorized by a small ANSI helper
(:mod:`mew._console`). Each command is a plain function with keyword args;
:func:`_build_parser` mirrors those args as ``add_argument`` calls and
:func:`main` dispatches via the parsed namespace.

The command functions are the CLI layer, not a supported Python API: each
keyword mirrors a flag, and the ``add_argument`` help text in the
``_add_*_cmd`` builders is the canonical documentation for what it does.
Drive benchmarks programmatically through :func:`mew.run` instead.
"""

from __future__ import annotations

import argparse
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
from mew._registry import compile_name_filter, narrow_entry

# BENCHMARK_VERSION is a git describe (`v1.9.5-74-ga8460680`), so it already
# carries the commit; the full SHA stays available as `mew.BENCHMARK_COMMIT`.
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

    # Per-selector filters and path-less stdin names OR together, then AND with
    # the global -k. Compiled up front so a bad pattern fails before any run.
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

        # Both profilers are Google Benchmark managers: GB drives them during the
        # run and stamps the results onto each Run, so there is no pre-pass and
        # no second execution of the suite.
        with ExitStack() as stack:
            memory_manager = None
            profiler_manager = None
            if profile_memory or flamegraph is not None:
                from mew import memory as _memory

                memory_manager = _memory.manager(stack)
            if sample or sample_html is not None:
                from mew import cpu as _cpu

                _cpu.require_pyinstrument()
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
    from mew._statistics import resolve_statistic
    from mew.compare import compare as _compare

    # One config walk for the whole command: the statistic default and the
    # regression gate both come out of the same [tool.mew] resolution.
    cfg_file = _load_config_or_exit()
    # --statistic wins; else fall back to [tool.mew] statistic; else stdlib median.
    spec = statistic if statistic is not None else _load_config_or_exit().statistic
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

        cfg = load_config(
            default_threshold=regression_threshold if regression_threshold is not None else 5.0,
            path=regressions_config,
            root=cfg_file.project_root,
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
      is set, keeping pipes, CI logs, and the docs ``--help`` capture clean.
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


def _warmup_seconds(value: str) -> float:
    """argparse type for --min-warmup-time: '0.2', '200ms', '1m' → seconds."""
    dur = value.strip()
    try:
        if dur.endswith("ms"):  # before "m": "500ms" is not minutes
            return float(dur[:-2]) / 1000
        if dur.endswith("m"):
            return float(dur[:-1]) * 60
        return float(dur.removesuffix("s"))
    except ValueError:
        # ArgumentTypeError gets argparse's usage-error exit (2); SystemExit
        # would exit 1, colliding with the "nothing matched" code.
        raise argparse.ArgumentTypeError(
            f"invalid --min-warmup-time {dur!r}; use seconds ('10s', '0.5'), "
            "milliseconds ('500ms'), or minutes ('1m')"
        ) from None


def _percent(value: str) -> float:
    """argparse type for --regression-threshold: '5%' → 5.0. Requires the '%' suffix
    so the flag reads unambiguously at the call site, not just in --help."""
    try:
        if not value.endswith("%"):
            raise ValueError
        return float(value[:-1])
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a percent like '5%', got {value!r}") from None


def _add_filter_args(
    p: argparse.ArgumentParser,
    *,
    pattern_help: str,
    literal_help: str = "Match -k as a literal string.",
) -> None:
    """Add the coupled ``-k/--pattern`` + ``-F/--literal`` pair.

    ``-F`` only means anything alongside ``-k``, so the two are always registered
    together; only the help text differs per command.
    """
    p.add_argument("-k", "--pattern", help=pattern_help)
    p.add_argument("-F", "--literal", action="store_true", help=literal_help)


def _add_tag_arg(p: argparse.ArgumentParser) -> None:
    """Add the shared ``-t/--tag`` filter (identical across list and run)."""
    p.add_argument(
        "-t",
        "--tag",
        action="append",
        default=[],
        help="Filter benchmarks by tag. Repeatable, OR semantics.",
    )


def _add_list_cmd(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "list",
        aliases=["ls"],
        help="List discovered benchmarks.",
        formatter_class=_CommandHelpFormatter,
    )
    p.add_argument("paths", nargs="*", default=[], help=_PATHS_HELP)
    _add_filter_args(
        p,
        pattern_help="List benchmarks whose name matches this regex (re.search, so a plain "
        "word works as a substring). A family case also matches by its `name[label]` "
        "form; pass --literal to match `[...]` without escaping.",
        literal_help="Match -k as a literal string, not a regex (e.g. paste `bench_sort[n=1000]`).",
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
        help="Print the bare name without the `file.py::` prefix. Path-free, so "
        "`mew list -n | mew run --stdin` round-trips from any directory.",
    )
    p.set_defaults(_func=list_)


def _add_run_cmd(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "run", help="Discover and run benchmarks.", formatter_class=_CommandHelpFormatter
    )
    p.add_argument("paths", nargs="*", default=[], help=_PATHS_HELP)
    _add_filter_args(
        p,
        pattern_help="Only run benchmarks whose name matches this regex (re.search). A family "
        "case also matches by its `name[label]` form; pass --literal to match `[...]`.",
    )
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
        "`<path>.json` / `<path>.jsonl` / `<path>.jsonl.gz` for a file. Default: `-`.",
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
    p.add_argument(
        "--min-warmup-time",
        type=_warmup_seconds,
        help="Warmup time per benchmark before measurement starts "
        "(seconds, or a duration like `200ms`).",
    )
    p.add_argument("--repetitions", type=int, metavar="<N>", help="Repeat each benchmark N times.")
    p.add_argument(
        "--random-interleaving",
        action="store_true",
        help="Randomly interleave repetitions across benchmarks to decorrelate "
        "thermal/load drift (effective with --repetitions > 1).",
    )
    p.add_argument(
        "--session-tag",
        help="Label this run's output as a session (e.g. `before`), addressable "
        "later as `mew compare results.jsonl@before`. Runs sharing a tag are "
        "compared as one session.",
    )
    p.add_argument(
        "--append",
        action="store_true",
        help="Append as a new session to existing `.jsonl[.gz]` sinks.",
    )
    p.add_argument(
        "--strict",
        action="store_true",
        help="Error instead of skipping when threaded benchmarks (threads / "
        "thread_range) are selected on a GIL interpreter, where they can't run.",
    )
    p.add_argument(
        "--profile-memory",
        action="store_true",
        help="Profile memory allocations with `memray`, via Google Benchmark's memory "
        "manager (an extra untimed pass per repetition).",
    )
    p.add_argument(
        "--flamegraph",
        type=Path,
        help="Write an HTML allocation flame graph to this path. Implies --profile-memory.",
    )
    p.add_argument(
        "--sample",
        action="store_true",
        help="Sample CPU in-process with `pyinstrument` (Python frames).",
    )
    p.add_argument(
        "--sample-interval",
        type=float,
        default=1e-4,
        help="pyinstrument sampling interval in seconds (default 1e-4).",
    )
    p.add_argument(
        "--sample-html",
        type=Path,
        help="Write a pyinstrument HTML report to this path. Implies --sample.",
    )
    p.set_defaults(_func=run)


def _add_compare_cmd(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "compare", help="Compare benchmark result files.", formatter_class=_CommandHelpFormatter
    )
    p.add_argument("files", nargs="+", type=Path, help="Result files; the last is the baseline.")
    p.add_argument(
        "-m",
        "--metric",
        default="real_time",
        help="Metric: real_time, cpu_time, iterations, or (for --profile-memory results) "
        "memory.peak_bytes / memory.allocations_per_iteration.",
    )
    p.add_argument(
        "--key",
        help="How benchmarks are matched: `name` (full) or `func` (strip the `file.py::` "
        "prefix). Defaults to `func` with --by, `name` otherwise.",
    )
    _add_filter_args(p, pattern_help="Regex filter (re.search).")
    p.add_argument("--stddev", action="store_true", help="Show stddev columns if present.")
    p.add_argument(
        "--by",
        help="Pivot one file on a field instead of comparing files, e.g. "
        "`custom.engine` (set per suite with mew.set_context).",
    )
    p.add_argument("--baseline", help="With --by, the baseline column (default: first written).")
    p.add_argument(
        "--statistic",
        help="Reducer over per-repetition values, for display and the regression gate: "
        "min, max, mean, median, gmean, or a pNN percentile like p95. "
        "Default: median. Overrides [tool.mew] statistic.",
    )
    p.add_argument(
        "--regression-threshold",
        type=_percent,
        metavar="<N%>",
        help="Regression magnitude that triggers a REGRESSED verdict, e.g. `5%%`. "
        "Always prints the regression panel; pair with --exit-non-zero-on-regression "
        "to also fail the command. Defaults to [tool.mew.regressions] default_threshold.",
    )
    p.add_argument(
        "--exit-non-zero-on-regression",
        action="store_true",
        help="Exit 2 if any benchmark regressed past the threshold (the "
        "[tool.mew.regressions] default when no --regression-threshold is given). "
        "Without this, the regression panel is informational only and the exit "
        "code is unaffected.",
    )
    p.add_argument(
        "--regressions-config",
        type=Path,
        help="TOML file with [tool.mew.regressions] (default: ./pyproject.toml).",
    )
    p.set_defaults(_func=compare)


def _add_completions_cmd(sub: argparse._SubParsersAction) -> None:
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


def _build_parser() -> argparse.ArgumentParser:
    """Build the argparse command tree. Each subparser sets ``_func`` to its handler."""
    parser = argparse.ArgumentParser(
        prog="mew",
        description="Microbenchmarking for Python via Google Benchmark.",
        formatter_class=_CommandHelpFormatter,
        # git-style: global options up front, then `<command> [<args>]`, instead
        # of argparse's default `{list,ls,run,…} ...` enumeration.
        usage="mew [-h] [--version] <command> [<args>]",
    )
    parser.add_argument("--version", action="version", version=_VERSION)
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
    # Namespace dests mirror each command's keyword params; `_command`/`_func`
    # are internal and excluded.
    kwargs = {k: v for k, v in vars(args).items() if not k.startswith("_")}
    func(**kwargs)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

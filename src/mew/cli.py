"""cyclopts CLI: `mew run`, `mew list`."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Annotated

from cyclopts import App, Parameter

from mew import config as _config
from mew import discovery, runner
from mew._registry import REGISTRY, Entry
from mew.reporter import JSONReporter, ParquetReporter, Reporter, RichReporter

app = App(name="mew", help="Microbenchmarking for Python via Google Benchmark.")


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

    selectors = [discovery.parse(p) for p in paths]
    files = discovery.collect_files(selectors, file_patterns=cfg.python_files)

    REGISTRY.clear()
    for f in files:
        discovery.import_file(f)

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


@app.command(name=["list", "ls"])
def list_(
    paths: Annotated[list[str], Parameter(name="paths")] = [],  # noqa: B006
    *,
    pattern: Annotated[
        str | None, Parameter(name=["--pattern", "-k"], help="substring filter")
    ] = None,
    tag: Annotated[
        list[str],
        Parameter(
            name=["--tag", "-t"],
            help="filter by tag (repeatable, OR semantics)",
        ),
    ] = [],  # noqa: B006
    show_tags: Annotated[bool, Parameter(help="print tags alongside each benchmark name")] = False,
) -> None:
    """List discovered benchmarks without running them."""
    entries = _collect(paths, pattern=pattern, tags=tag or None)
    if not entries:
        print("no benchmarks found", file=sys.stderr)
        raise SystemExit(1)
    for e in entries:
        if show_tags:
            tags_str = ",".join(e.tags) if e.tags else "-"
            print(f"{e.name}\t[{tags_str}]")
        else:
            print(e.name)


_STDOUT_SENTINELS = frozenset({"-", "stdout"})


def _build_reporters(outputs: list[str]) -> list[Reporter]:
    """Resolve `-o` sinks into a list of reporters.

    Sentinels: `-` or `stdout` (terminal, rich) and `*.json` (JSON file).
    Default when no `-o` is provided is a single rich reporter on stdout.

    Note: cyclopts treats a lone `-` as a flag, so prefer `--output=-` /
    `-o stdout` from the shell. `stdout` is also accepted to sidestep
    quoting hassles.
    """
    if not outputs:
        return [RichReporter()]

    reps: list[Reporter] = []
    seen_stdout = False
    seen_files: set[Path] = set()
    for raw in outputs:
        if raw in _STDOUT_SENTINELS:
            if seen_stdout:
                print("duplicate stdout sink", file=sys.stderr)
                raise SystemExit(2)
            seen_stdout = True
            reps.append(RichReporter())
            continue
        path = Path(raw).resolve()
        if path in seen_files:
            print(f"duplicate file sink: {raw}", file=sys.stderr)
            raise SystemExit(2)
        seen_files.add(path)
        suffix = path.suffix.lower()
        if suffix == ".json":
            reps.append(JSONReporter(output=Path(raw)))
        elif suffix in (".parquet", ".pq"):
            reps.append(ParquetReporter(output=Path(raw)))
        else:
            print(
                f"unsupported output format: {raw} (use `-`/`stdout`, *.json, *.parquet, or *.pq)",
                file=sys.stderr,
            )
            raise SystemExit(2)
    return reps


@app.command
def run(
    paths: Annotated[list[str], Parameter(name="paths")] = [],  # noqa: B006
    *,
    pattern: Annotated[
        str | None, Parameter(name=["--pattern", "-k"], help="substring filter")
    ] = None,
    tag: Annotated[
        list[str],
        Parameter(
            name=["--tag", "-t"],
            help="filter by tag (repeatable, OR semantics)",
        ),
    ] = [],  # noqa: B006
    output: Annotated[
        list[str],
        Parameter(
            name=["--output", "-o"],
            help="output sink, repeatable: `-` for a rich terminal table, "
            "`<path>.json` for a JSON file. Default: `-`.",
        ),
    ] = [],  # noqa: B006
    min_time: Annotated[
        str | None,
        Parameter(help="GB --benchmark_min_time: seconds (`0.5`) or fixed iters (`100x`)"),
    ] = None,
    repetitions: Annotated[int | None, Parameter(help="repeat each benchmark N times")] = None,
    extra: Annotated[
        list[str],
        Parameter(name="--gb", help="raw arg forwarded to Google Benchmark"),
    ] = [],  # noqa: B006
) -> None:
    """Discover and run benchmarks."""
    entries = _collect(paths, pattern=pattern, tags=tag or None)
    if not entries:
        print("no benchmarks found", file=sys.stderr)
        raise SystemExit(1)

    argv: list[str] = ["mew"]
    if min_time is not None:
        argv.append(f"--benchmark_min_time={min_time}")
    if repetitions is not None:
        argv.append(f"--benchmark_repetitions={repetitions}")
    argv.extend(extra)

    reporters = _build_reporters(output)
    runner.run(entries, argv=argv, reporter=reporters)


if __name__ == "__main__":
    app()

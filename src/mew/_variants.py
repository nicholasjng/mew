"""Orchestrate ``mew run --variant``: one subprocess per (variant × repetition).

For "same logical suite, N mutually-incompatible processes": rival engines that
statically link the same library, GIL vs free-threaded interpreters, Python
versions, ASAN vs Release builds. Each variant runs in its own child
(:mod:`mew._variant_worker`); this orchestrator is the single writer.

It generates one ``session_id``, drives children in **repetition-major** order
(rep0: A B, rep1: A B, …) so thermal/load drift decorrelates from the variant
axis, and re-stamps each child's JSONL rows with the shared session, variant
name, and repetition index before fanning out to the real reporters. A child row
already is a :class:`~mew._typing.RunRow`, so the merge is a dict overlay.
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mew._session import new_session_id
from mew._typing import RunRow
from mew.reporter import Reporter
from mew.runner import _to_single_reporter


@dataclass(frozen=True, slots=True)
class ProfileConfig:
    """Profiling flags forwarded to each variant child (mirrors ``mew run``).

    Empty by default (no profiling). HTML artifact paths (``flamegraph``,
    ``sample_html``) are suffixed with the variant name per child so the
    variants don't overwrite each other's output.
    """

    profile_memory: bool = False
    flamegraph: Path | None = None
    memory_iterations: int = 100
    sample: bool = False
    sample_interval: float = 1e-4
    sample_iterations: int = 1000
    sample_html: Path | None = None

    @property
    def enabled(self) -> bool:
        return (
            self.profile_memory
            or self.sample
            or self.flamegraph is not None
            or self.sample_html is not None
        )


def _variant_artifact(path: Path | None, variant: str) -> Path | None:
    """``out.html`` → ``out.<variant>.html`` so per-variant artifacts don't clobber."""
    if path is None:
        return None
    return path.with_name(f"{path.stem}.{variant}{path.suffix}")


def _merge_row(
    child_row: RunRow,
    *,
    variant: str,
    rep: int,
    outer_reps: int,
    custom: dict[str, Any] | None,
) -> RunRow:
    """Overlay ``variant`` and merged repetition identity onto a child's
    :class:`~mew._typing.RunRow`.

    A child may itself run N inner Google Benchmark repetitions (via
    ``--benchmark_repetitions`` in the forwarded args or a decorator option), so
    the merged index composes outer × inner — a flat overwrite with the outer
    index would collapse distinct inner measurements onto one index. With a
    single inner repetition this reduces to ``repetition_index=rep``,
    ``repetitions=outer_reps``.

    The child's ``custom`` preserves each variant's context in the merged file
    (the single top-level context block holds only one).
    """
    inner_total = max(1, int(child_row.get("repetitions") or 1))
    inner_idx = child_row.get("repetition_index")
    merged: RunRow = {
        **child_row,
        "variant": variant,
        "repetitions": outer_reps * inner_total,
        # Aggregate rows carry no per-repetition index; anchor them at the rep.
        "repetition_index": rep * inner_total
        + (inner_idx if isinstance(inner_idx, int) and inner_idx >= 0 else 0),
    }
    if custom:
        merged["custom"] = custom
    return merged


def _pseudo_raw_context(
    child_ctx: dict[str, Any],
    session_id: str,
    session_tag: str | None,
) -> dict[str, Any]:
    """Rebuild a raw-C++-shaped context from a child's projected context block.

    Reporters re-project via ``_build_context`` (which reads ``cpu_scaling`` as
    a string), so undo the child's projection and graft on the shared session
    identity.
    """
    raw: dict[str, Any] = {
        "host_name": child_ctx.get("host_name"),
        "executable": child_ctx.get("executable"),
        "num_cpus": child_ctx.get("num_cpus"),
        "mhz_per_cpu": child_ctx.get("mhz_per_cpu"),
        "cpu_scaling": "enabled" if child_ctx.get("cpu_scaling_enabled") else "disabled",
        "library_build_type": child_ctx.get("library_build_type"),
        "session_id": session_id,
    }
    if session_tag:
        raw["session_tag"] = session_tag
    if child_ctx.get("custom"):
        raw["custom"] = child_ctx["custom"]
    return raw


def _profile_args(profiling: ProfileConfig, variant: str) -> list[str]:
    """Build the worker's profiling flags, suffixing artifact paths per variant."""
    if not profiling.enabled:
        return []
    args: list[str] = []
    if profiling.profile_memory or profiling.flamegraph is not None:
        args.append(f"--memory-iterations={profiling.memory_iterations}")
    if profiling.profile_memory:
        args.append("--profile-memory")
    if (fg := _variant_artifact(profiling.flamegraph, variant)) is not None:
        args.append(f"--flamegraph={fg}")
    if profiling.sample:
        args.append("--sample")
    if profiling.sample or profiling.sample_html is not None:
        args.append(f"--sample-interval={profiling.sample_interval}")
        args.append(f"--sample-iterations={profiling.sample_iterations}")
    if (sh := _variant_artifact(profiling.sample_html, variant)) is not None:
        args.append(f"--sample-html={sh}")
    return args


def _run_child(
    file: Path,
    pattern: str | None,
    literal: bool,
    tags: list[str],
    min_time: str | None,
    min_warmup_time: float | None,
    random_interleaving: bool,
    profiling: ProfileConfig,
    variant: str,
) -> tuple[dict[str, Any], list[RunRow]] | None:
    """Run one variant child; return its (context, rows) or None on failure."""
    # `--opt=value` form: a min_time like `20x` would otherwise be read by
    # argparse as the next option.
    cmd = [sys.executable, "-m", "mew._variant_worker", f"--file={file}"]
    if pattern:
        cmd.append(f"--pattern={pattern}")
    if literal:
        cmd.append("--literal")
    cmd += [f"--tag={t}" for t in tags]
    if min_time is not None:
        cmd.append(f"--min-time={min_time}")
    if min_warmup_time is not None:
        cmd.append(f"--min-warmup-time={min_warmup_time}")
    if random_interleaving:
        cmd.append("--random-interleaving")
    cmd += _profile_args(profiling, variant)

    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        detail = proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else "no output"
        print(f"warning: variant child failed ({file}): {detail}", file=sys.stderr)
        return None

    ctx: dict[str, Any] = {}
    rows: list[RunRow] = []
    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        obj = json.loads(line)
        if "name" in obj:
            rows.append(obj)
        else:
            ctx = obj.get("context", obj) or ctx
    return ctx, rows


def run_variants(
    variants: dict[str, Path],
    *,
    reporters: list[Reporter],
    pattern: str | None = None,
    literal: bool = False,
    tags: list[str] | None = None,
    min_time: str | None = None,
    min_warmup_time: float | None = None,
    random_interleaving: bool = False,
    repetitions: int = 1,
    session_tag: str | None = None,
    profiling: ProfileConfig | None = None,
) -> int:
    """Run each variant in its own subprocess, merging rows into ``reporters``.

    Returns the number of failed child invocations (0 on full success); the CLI
    turns a nonzero count into a nonzero exit while keeping all rows that did
    land.
    """
    session_id = new_session_id()
    reporter = _to_single_reporter(reporters)
    order = list(variants)
    profiling = profiling or ProfileConfig()
    started = False
    failures = 0

    outer_reps = max(1, repetitions)
    no_profiling = ProfileConfig()

    # Repetition-major: rep0 over all variants, then rep1, … (A B A B …).
    for rep in range(outer_reps):
        for name in order:
            # Profiling is an out-of-loop pass with a fixed per-variant artifact
            # path: run it on the first repetition only, or every later rep
            # would redo the expensive pass just to overwrite the same file.
            result = _run_child(
                variants[name],
                pattern,
                literal,
                tags or [],
                min_time,
                min_warmup_time,
                random_interleaving,
                profiling if rep == 0 else no_profiling,
                name,
            )
            if result is None:
                failures += 1
                continue
            child_ctx, rows = result
            if not started:
                raw = _pseudo_raw_context(child_ctx, session_id, session_tag)
                if reporter is not None and reporter.report_context(raw) is False:
                    # A veto can arrive after sinks already opened resources in
                    # report_context (Fanout calls every child): close them.
                    if fn := getattr(reporter, "finalize", None):
                        fn()
                    return failures + 1
                started = True
            if reporter is not None:
                # Overlay variant / rep identity and the child's custom context.
                child_custom = child_ctx.get("custom")
                reporter.report_runs(
                    [
                        _merge_row(
                            row, variant=name, rep=rep, outer_reps=outer_reps, custom=child_custom
                        )
                        for row in rows
                    ]
                )

    if started and reporter is not None and (fn := getattr(reporter, "finalize", None)):
        fn()
    if not started:
        print("mew: no variant produced any benchmarks", file=sys.stderr)
        return failures or 1
    return failures

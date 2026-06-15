"""Orchestrate ``mew run --variant``: one subprocess per (variant × repetition).

The shape this serves: "same logical suite, N mutually-incompatible processes"
— engines that statically link the same library, GIL vs free-threaded
interpreters, Python versions, ASAN vs Release builds. Each variant runs in its
own child (:mod:`mew._variant_worker`); this orchestrator is the single writer.

It generates one ``session_id`` for the whole run, then drives children in
**repetition-major** order (rep0: A B, rep1: A B, …) so thermal/load drift
decorrelates from the variant axis instead of contaminating it. Each child
streams JSONL on stdout; the orchestrator re-stamps every row with the shared
session id, the ``variant`` name, and the repetition index, then fans the rows
out to the user's real reporters — so the live console, JSON, JSONL, and
Parquet sinks all work unchanged via :class:`_DictRun`.
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from mew._session import new_session_id
from mew.reporter import Reporter
from mew.runner import _to_single_reporter

if TYPE_CHECKING:
    from mew._core import Run


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


class _Enumish:
    """Stand-in for a C++ enum field: only ``.name`` is read by the reporters."""

    __slots__ = ("name",)

    def __init__(self, name: str) -> None:
        self.name = name


def _as_dataclass(cls: Any, d: dict[str, Any] | None) -> Any:
    """Rebuild a flat profile dataclass from a child row's serialized block.

    The child emitted ``dataclasses.asdict(profile)`` into its JSONL row; undo
    that so the parent's reporters re-serialize it the same as a direct run
    (``_run_to_dict`` / ``ParquetReporter._row`` expect a dataclass, not a dict).
    """
    return cls(**d) if d else None


class _DictRun:
    """A faux :class:`~mew._core.Run` backed by a child's JSONL row dict.

    The child row is exactly :func:`mew.reporter._run_to_dict` output, so this
    re-exposes the attributes the reporters read, overriding ``variant`` and
    ``repetition_index`` with the orchestration's values. Lets merged variant
    rows flow through every existing reporter without a parallel write path.
    """

    def __init__(
        self,
        row: dict[str, Any],
        *,
        variant: str,
        repetition_index: int,
        custom: dict[str, Any] | None = None,
    ) -> None:
        from mew.cpu import CPUProfile
        from mew.memory import MemoryProfile

        self._d = row
        self.variant = variant
        self._rep = repetition_index
        # The child's per-suite set_context() values, kept per variant so the
        # merged file records each variant's own engine/version (the single
        # top-level context block can hold only one).
        self.custom = custom
        # Profiles arrive as plain dicts in the child row (--variant + --profile-*);
        # rebuild the dataclasses so the parent's reporters serialize them natively.
        self.memory = _as_dataclass(MemoryProfile, row.get("memory"))
        self.cpu = _as_dataclass(CPUProfile, row.get("cpu_profile"))

    def benchmark_name(self) -> str:
        return self._d["name"]

    def adjusted_real_time(self) -> float:
        return self._d["real_time"]

    def adjusted_cpu_time(self) -> float:
        return self._d["cpu_time"]

    @property
    def run_name(self) -> str:
        return self._d["run_name"]

    @property
    def run_type(self) -> _Enumish:
        return _Enumish(self._d["run_type"])

    @property
    def time_unit(self) -> _Enumish:
        return _Enumish(self._d["time_unit"])

    @property
    def repetition_index(self) -> int:
        return self._rep

    @property
    def counters(self) -> dict[str, float]:
        return self._d.get("counters") or {}

    def __getattr__(self, name: str) -> Any:
        # Remaining scalar fields map 1:1 to row keys (family_index, threads,
        # iterations, aggregate_name, report_label/label, skipped, …).
        d = self._d
        if name == "report_label":
            return d.get("label", "")
        if name in d:
            return d[name]
        raise AttributeError(name)


def _pseudo_raw_context(
    child_ctx: dict[str, Any],
    session_id: str,
    session_tag: str | None,
    variant_order: list[str],
) -> dict[str, Any]:
    """Rebuild a raw-C++-shaped context from a child's projected context block.

    Reporters re-project via ``_build_context`` (which reads ``cpu_scaling`` as
    a string), so undo the child's projection and graft on the shared session
    identity and the declared variant order.
    """
    raw: dict[str, Any] = {
        "host_name": child_ctx.get("host_name"),
        "executable": child_ctx.get("executable"),
        "num_cpus": child_ctx.get("num_cpus"),
        "mhz_per_cpu": child_ctx.get("mhz_per_cpu"),
        "cpu_scaling": "enabled" if child_ctx.get("cpu_scaling_enabled") else "disabled",
        "library_build_type": child_ctx.get("library_build_type"),
        "session_id": session_id,
        "variants": variant_order,
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
    tags: list[str],
    gb_args: list[str],
    profiling: ProfileConfig,
    variant: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
    """Run one variant child; return its (context, rows) or None on failure."""
    # `--opt=value` form (not `--opt value`): GB args like `--benchmark_min_time=20x`
    # start with `--`, which argparse would otherwise read as the next option.
    cmd = [sys.executable, "-m", "mew._variant_worker", f"--file={file}"]
    if pattern:
        cmd.append(f"--pattern={pattern}")
    cmd += [f"--tag={t}" for t in tags]
    cmd += [f"--gb={g}" for g in gb_args]
    cmd += _profile_args(profiling, variant)

    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        detail = proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else "no output"
        print(f"warning: variant child failed ({file}): {detail}", file=sys.stderr)
        return None

    ctx: dict[str, Any] = {}
    rows: list[dict[str, Any]] = []
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
    gb_args: list[str],
    pattern: str | None = None,
    tags: list[str] | None = None,
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

    # Repetition-major: rep0 over all variants, then rep1, … (A B A B …).
    for rep in range(max(1, repetitions)):
        for name in order:
            result = _run_child(variants[name], pattern, tags or [], gb_args, profiling, name)
            if result is None:
                failures += 1
                continue
            child_ctx, rows = result
            if not started:
                raw = _pseudo_raw_context(child_ctx, session_id, session_tag, order)
                if reporter is not None and reporter.report_context(raw) is False:
                    return failures + 1
                started = True
            if reporter is not None:
                # _DictRun is a structural Run stand-in (see its docstring); the
                # reporters only touch the duck-typed surface it re-exposes. Each
                # variant's own custom context rides along per row so it survives
                # the single shared top-level context block.
                child_custom = child_ctx.get("custom")
                reporter.report_runs(
                    cast(
                        "list[Run]",
                        [
                            _DictRun(row, variant=name, repetition_index=rep, custom=child_custom)
                            for row in rows
                        ],
                    )
                )

    if started and reporter is not None and (fn := getattr(reporter, "finalize", None)):
        fn()
    if not started:
        print("mew: no variant produced any benchmarks", file=sys.stderr)
        return failures or 1
    return failures

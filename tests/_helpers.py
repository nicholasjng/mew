"""Shared test helpers: result-file builders, a capture terminal, a capture reporter.

The row/write_* builders encode the on-disk result-file contract (the shape
mew's JSON/JSONL sinks write) in one place; the compare and regressions tests
both consume it.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

from mew._console import Terminal


class Console(Terminal):
    """A capture terminal: renders into a buffer, color off.

    Mirrors the ``export_text()`` shape of rich's recording console, which
    these tests were originally written against.
    """

    def __init__(self, *, width: int = 80) -> None:
        self._buf = io.StringIO()
        super().__init__(file=self._buf, width=width, color=False)

    def export_text(self) -> str:
        return self._buf.getvalue()


class Capture:
    """Minimal Reporter that stashes context, rows, and the finalize call.

    The canonical fake for tests that only observe what a reporter receives;
    scenario-shaped fakes (a raising callback, a missing ``finalize``, ...) stay
    local to their test.
    """

    def __init__(self) -> None:
        self.context: dict[str, Any] | None = None
        self.runs: list[Any] = []
        self.finalized = False

    def report_context(self, context: dict[str, Any]) -> None:
        self.context = context

    def report_runs(self, runs: list[Any]) -> None:
        self.runs.extend(runs)

    def finalize(self) -> None:
        self.finalized = True


#: Flat kwargs the tests spell, mapped into the row's `session` block.
_SESSION_KEYS = {"session_id": "id", "session_tag": "tag", "date": "date", "host_name": "host"}


def row(name: str, real_time: float, **extra: Any) -> dict:
    """A minimal per-repetition result row, as mew's sinks write it.

    Session identity and provenance are spelled flat here (``session_id=``,
    ``host_name=``, ``custom=``) and folded into the stamped ``session`` /
    ``context`` blocks, so a test reads as one row rather than nested literals.
    """
    session = {dst: extra.pop(src) for src, dst in _SESSION_KEYS.items() if src in extra}
    context = extra.pop("custom", None)
    out: dict[str, Any] = {
        "name": name,
        "real_time": real_time,
        "cpu_time": real_time,
        "iterations": 1000,
        "time_unit": "ns",
        "aggregate_name": "",
    }
    if session:
        out["session"] = session
    if context is not None:
        out["context"] = context
    return {**out, **extra}


def write_json(path: Path, benches: list[dict], context: dict | None = None) -> None:
    """Write a single-document JSON result file (the JSONReporter shape)."""
    path.write_text(json.dumps({"context": context or {}, "benchmarks": benches}))


def write_jsonl(path: Path, benches: list[dict], context: dict | None = None) -> None:
    """Write a JSONL result file with a leading context line (the channel shape)."""
    lines = [json.dumps({"context": context or {}})]
    lines += [json.dumps(b) for b in benches]
    path.write_text("\n".join(lines) + "\n")


def write_pair(
    tmp_path: Path,
    *,
    other: list[dict],
    base: list[dict],
    other_context: dict | None = None,
    base_context: dict | None = None,
    suffix: str = ".json",
) -> tuple[Path, Path]:
    """Write an ``(other, base)`` result-file pair for two-file compare tests.

    Returned in ``compare([other, base])`` argument order (the CLI convention:
    baseline last), so call sites read ``other, base = write_pair(...)``.
    """
    writer = write_jsonl if suffix.endswith(".jsonl") else write_json
    other_path = tmp_path / f"other{suffix}"
    base_path = tmp_path / f"base{suffix}"
    writer(other_path, other, context=other_context)
    writer(base_path, base, context=base_context)
    return other_path, base_path

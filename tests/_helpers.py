"""Shared test helpers: result-file builders and a capture terminal.

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


def row(name: str, real_time: float, **extra: Any) -> dict:
    """A minimal per-repetition result row, as mew's sinks write it."""
    return {
        "name": name,
        "real_time": real_time,
        "cpu_time": real_time,
        "iterations": 1000,
        "time_unit": "ns",
        "aggregate_name": "",
        **extra,
    }


def write_json(path: Path, benches: list[dict], context: dict | None = None) -> None:
    """Write a single-document JSON result file (the JSONReporter shape)."""
    path.write_text(json.dumps({"context": context or {}, "benchmarks": benches}))


def write_jsonl(path: Path, benches: list[dict], context: dict | None = None) -> None:
    """Write a JSONL result file with a leading context line (the channel shape)."""
    lines = [json.dumps({"context": context or {}})]
    lines += [json.dumps(b) for b in benches]
    path.write_text("\n".join(lines) + "\n")

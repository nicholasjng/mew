"""On-disk completion cache: benchmark names/cases/tags keyed by bench-file mtimes.

Tab completion must never import bench files — it's slow, and a `uv tool`-installed
`mew` lacks the project's deps. So `mew run`/`list`/`profile` write this cache as a
side effect of their normal discovery, and `mew __complete` only ever reads it.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from mew._registry import Entry

_VERSION = 1


def _cache_path(project_root: Path) -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    key = hashlib.sha256(str(project_root.resolve()).encode()).hexdigest()[:16]
    return Path(base) / "mew" / "completions" / f"{key}.json"


def _signature(files: list[Path]) -> list[list[object]]:
    """Freshness key: sorted (path, mtime_ns, size). A new/removed/edited file shifts it."""
    sig: list[list[object]] = []
    for f in sorted(files):
        try:
            st = f.stat()
        except OSError:
            continue
        sig.append([str(f), st.st_mtime_ns, st.st_size])
    return sig


@dataclass(slots=True)
class CacheData:
    names: list[str]  # bare func names (the part after `file.py::`)
    cases: list[str]  # `func[label]` forms
    tags: list[str]


def build(entries: list[Entry]) -> CacheData:
    names: list[str] = []
    cases: list[str] = []
    tags: set[str] = set()
    for e in entries:
        bare = e.name.rsplit("::", 1)[-1]
        names.append(bare)
        for label in e.case_labels or ():
            cases.append(f"{bare}[{label}]")
        tags.update(e.tags)
    # dedupe, preserve discovery order
    return CacheData(list(dict.fromkeys(names)), list(dict.fromkeys(cases)), sorted(tags))


def write(project_root: Path, files: list[Path], data: CacheData) -> None:
    path = _cache_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": _VERSION,
        "signature": _signature(files),
        "names": data.names,
        "cases": data.cases,
        "tags": data.tags,
    }
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(payload, fh)
        os.replace(tmp, path)  # atomic; survives concurrent Tab reads
    except BaseException:
        os.unlink(tmp)
        raise


def read_fresh(project_root: Path, files: list[Path]) -> CacheData | None:
    """Return cached data iff the bench-file signature still matches; else ``None``."""
    try:
        payload = json.loads(_cache_path(project_root).read_text())
    except (OSError, ValueError):
        return None
    if payload.get("version") != _VERSION or payload.get("signature") != _signature(files):
        return None
    return CacheData(payload["names"], payload["cases"], payload["tags"])


def refresh(project_root: Path, files: list[Path], entries: list[Entry]) -> None:
    """Best-effort cache write from a completed discovery. Never raises into the caller."""
    with contextlib.suppress(OSError):
        write(project_root, files, build(entries))

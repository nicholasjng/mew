"""Dotted paths into context, such as ``vcs.commit``.

A dot always nests, as in DuckDB. A key that itself holds a dot is quoted:
``context."a.b"``, with ``""`` for a quote inside one.
"""

from __future__ import annotations

import csv
from typing import Any

Path = tuple[str, ...]


def parse_path(text: str) -> Path:
    """Split a dotted path into its keys, honoring ``"quoted"`` keys as CSV does.

    Raises
    ------
    ValueError
        If a key is empty, or a quote doesn't wrap a whole key.
    """
    try:
        keys = next(csv.reader([text], delimiter=".", strict=True))
    except csv.Error:
        raise ValueError(
            f'malformed path {text!r}\nhint: quote a whole key, as in context."a.b"'
        ) from None
    if not keys or "" in keys:  # "" reads as no keys at all
        raise ValueError(f"empty key in path {text!r}")
    return tuple(keys)


def format_path(path: Path) -> str:
    """Join keys with dots, quoting a key that holds a dot, a quote or a line break."""
    return ".".join(
        '"' + k.replace('"', '""') + '"' if not k or any(c in k for c in '."\r\n') else k
        for k in path
    )


def split_assignment(text: str) -> tuple[Path, str]:
    """Split ``<path>=<value>`` at the first ``=`` outside a quoted key."""
    quoted = False
    for i, ch in enumerate(text):
        if ch == '"':
            quoted = not quoted
        elif ch == "=" and not quoted and i:
            return parse_path(text[:i]), text[i + 1 :]
    raise ValueError(f"expected <path>=<value>, got {text!r}")


def set_path(obj: dict[str, Any], path: Path, value: Any) -> None:
    """Set ``value`` at ``path``, creating mappings on the way and replacing other values."""
    for key in path[:-1]:
        child = obj.get(key)
        # A copy, so setting a value never changes a mapping the caller still holds.
        child = obj[key] = dict(child) if isinstance(child, dict) else {}
        obj = child
    obj[path[-1]] = value

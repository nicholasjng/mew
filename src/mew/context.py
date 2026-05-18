"""User-defined benchmark context.

A process-global ``dict[str, Any]`` populated via :func:`set_context` /
:func:`update_context`. Keys containing dots are interpreted as nested paths,
so ``set_context("dataset.size", 1024)`` ends up as ``{"dataset": {"size":
1024}}`` — convenient for SQL-style drill-downs (e.g. DuckDB's
``context.custom.dataset.size``).

A snapshot is merged into the reporter's context dict under ``ctx["custom"]``
at run time; the snapshot is taken when :func:`mew.run` starts so concurrent
mutations don't affect the in-flight run.
"""

from __future__ import annotations

import copy
from typing import Any

_CONTEXT: dict[str, Any] = {}


def _check_key(key: str) -> list[str]:
    if not isinstance(key, str) or not key:
        raise ValueError(f"context key must be a non-empty string, got {key!r}")
    parts = key.split(".")
    if any(not p for p in parts):
        raise ValueError(f"context key has empty path segment: {key!r}")
    return parts


def _set_nested(target: dict[str, Any], key: str, value: Any) -> None:
    parts = _check_key(key)
    cur: dict[str, Any] = target
    for i, part in enumerate(parts[:-1]):
        existing = cur.get(part)
        if existing is None:
            cur[part] = {}
        elif not isinstance(existing, dict):
            path = ".".join(parts[: i + 1])
            raise ValueError(
                f"cannot set context key {key!r}: existing value at {path!r} "
                f"is not a dict ({type(existing).__name__})"
            )
        cur = cur[part]
    cur[parts[-1]] = value


def set_context(key: str, value: Any) -> None:
    """Set a single value in the global benchmark context.

    Parameters
    ----------
    key : str
        Context key. Dotted names result in nested dicts
        (``"dataset.size"`` → ``{"dataset": {"size": ...}}``).
    value : Any
        Value to store. Reporters serialize this when emitting context,
        so JSON-friendly values are preferred.

    Raises
    ------
    ValueError
        If ``key`` is empty, has an empty path segment, or traverses through
        a non-dict value.
    """
    _set_nested(_CONTEXT, key, value)


def update_context(*mapping: dict[str, Any], **kwargs: Any) -> None:
    """Set many context values at once.

    Positional dicts are applied first, then ``kwargs``. Dotted keys nest in
    both forms; for dotted keys via splat, use
    ``update_context(**{"a.b": 1})``.

    Parameters
    ----------
    *mapping : dict[str, Any]
        Mappings whose items are applied in order.
    **kwargs
        Additional key-value pairs applied after the positional mappings.

    Raises
    ------
    ValueError
        Same conditions as :func:`set_context`.
    """
    for m in mapping:
        for k, v in m.items():
            _set_nested(_CONTEXT, k, v)
    for k, v in kwargs.items():
        _set_nested(_CONTEXT, k, v)


def get_context() -> dict[str, Any]:
    """Return a deep copy of the current global benchmark context.

    Returns
    -------
    dict[str, Any]
        Independent snapshot — mutating it does not affect future runs.
    """
    return copy.deepcopy(_CONTEXT)


def clear_context() -> None:
    """Drop every entry from the global benchmark context."""
    _CONTEXT.clear()


def _snapshot() -> dict[str, Any]:
    # Internal: used by the runner to snapshot at run start.
    return copy.deepcopy(_CONTEXT)


__all__ = [
    "clear_context",
    "get_context",
    "set_context",
    "update_context",
]

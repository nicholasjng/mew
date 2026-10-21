"""User-defined context that every benchmark run records.

The context is a process-global ``dict[str, Any]``, set with
:func:`set_context` and :func:`update_context`. Dotted keys nest, so
``set_context("dataset.size", 1024)`` yields ``{"dataset": {"size": 1024}}``.
Each run stores a snapshot of it in its ``context`` block.
"""

from __future__ import annotations

import copy
from typing import Any

_CONTEXT: dict[str, Any] = {}


def _set_nested(key: str, value: Any) -> None:
    if not isinstance(key, str) or not key:
        raise ValueError(f"context key must be a non-empty string, got {key!r}")
    parts = key.split(".")
    if any(not p for p in parts):
        raise ValueError(f"context key has empty path segment: {key!r}")
    cur: dict[str, Any] = _CONTEXT
    for part in parts[:-1]:
        # The last write wins, so a non-dict value on the path, None included,
        # gives way to a dict.
        if not isinstance(cur.get(part), dict):
            cur[part] = {}
        cur = cur[part]
    # A copy, so neither the caller's later changes nor a later dotted write
    # under this key reach the other side.
    cur[parts[-1]] = copy.deepcopy(value)


def set_context(key: str, value: Any) -> None:
    """Set one context value.

    The last write wins: setting ``"dataset"`` replaces everything nested under
    it, and setting ``"dataset.size"`` replaces a ``"dataset"`` value that is not
    a dict, ``None`` included, with a new dict.

    Parameters
    ----------
    key : str
        Context key. Dotted names nest (``"dataset.size"`` → ``{"dataset": {"size": ...}}``).
    value : Any
        Stored as a deep copy: changing the object afterwards doesn't change
        what runs record, and later writes to the context don't change the
        object. File reporters write it as JSON, falling back to ``str()``.

    Raises
    ------
    ValueError
        If ``key`` is empty or has an empty path segment.
    """
    _set_nested(key, value)


def update_context(*mapping: dict[str, Any], **kwargs: Any) -> None:
    """Set several context values: the mappings in order, then ``kwargs``.

    Each value is set as by :func:`set_context`, so dotted keys nest in both
    forms; pass them as keywords with ``update_context(**{"a.b": 1})``.

    Parameters
    ----------
    *mapping : dict[str, Any]
        Mappings to apply, in order.
    **kwargs
        Values applied after the mappings.

    Raises
    ------
    ValueError
        If a key is empty or has an empty path segment.
    """
    for m in (*mapping, kwargs):
        for k, v in m.items():
            _set_nested(k, v)


def get_context() -> dict[str, Any]:
    """Return a deep copy of the context.

    Returns
    -------
    dict[str, Any]
        A copy; changing it doesn't change what runs record.
    """
    return copy.deepcopy(_CONTEXT)


def clear_context() -> None:
    """Remove every context value."""
    _CONTEXT.clear()


__all__ = [
    "clear_context",
    "get_context",
    "set_context",
    "update_context",
]

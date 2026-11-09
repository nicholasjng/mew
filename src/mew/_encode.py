"""Encode user values for result rows, so a row never holds a ``str()`` of an object.

A value JSON represents as itself (``str``, ``int``, ``float``, ``bool``, ``None``)
is kept; anything else becomes its type's qualified name, such as
``"torch.Tensor"``. A tensor's ``str()`` can run to hundreds of kilobytes and
still not identify it; its type at least says what it was. ``float`` NaN and
infinities are kept and written as JSON's ``NaN`` and ``Infinity`` extensions.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

_SCALARS = (str, int, float, bool, type(None))


def type_name(value: Any) -> str:
    """``value``'s type as ``module.QualName``, e.g. ``"torch.Tensor"``."""
    cls = type(value)
    return f"{cls.__module__}.{cls.__qualname__}"


def encode_param(value: Any) -> Any:
    """A case parameter as a row holds it: a JSON scalar, or its type's name."""
    return value if isinstance(value, _SCALARS) else type_name(value)


def encode_context(value: Any) -> Any:
    """A context value as a row holds it.

    Scalars stay; mappings and lists or tuples are encoded item by item, a
    mapping's keys as strings; a path becomes its ``os.fspath``; anything else
    its type's qualified name.
    """
    if isinstance(value, _SCALARS):
        return value
    if isinstance(value, Mapping):
        return {_key(k): encode_context(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [encode_context(v) for v in value]
    if isinstance(value, os.PathLike):
        return os.fspath(value)
    return type_name(value)


def _key(key: Any) -> str:
    """A mapping key as JSON needs it, a string: as JSON itself writes a number key."""
    if isinstance(key, str):
        return key
    return str(key) if isinstance(key, _SCALARS) else type_name(key)

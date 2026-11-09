"""Load the managers ``mew run --cpu-profiler`` and ``--memory-profiler`` name.

Google Benchmark runs one manager per slot, so each slot has its flag, which
takes one value. A preset is a ``[tool.mew.profilers.<name>]`` table. Its
``class`` key names a class as ``module:attr`` or ``path.py:attr``. Every other
key is a keyword argument for that class, kebab-case turned into snake_case. A
flag takes a preset name, or a spec (anything with a ``:``) to run a class with
its defaults.

The contract for the class:

- mew calls ``cls(**options)``.
- The instance has its slot's methods: ``after_setup_start`` and
  ``before_teardown_stop`` for a CPU profiler (Google Benchmark's profiler
  manager), ``start`` and ``stop`` for a memory profiler. They must be defined
  on the class. Subclassing the protocol is optional.
- If the instance is a context manager, mew enters it for the run and keeps
  the instance itself, not what ``__enter__`` returns.

mew's own presets may name a function instead of a class, so ``sample`` can
keep choosing Tachyon or pyinstrument by interpreter.
"""

from __future__ import annotations

import difflib
import inspect
import pkgutil
import re
from collections.abc import Mapping
from contextlib import AbstractContextManager, ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, NamedTuple

from mew import _registry

Slot = Literal["cpu", "memory"]

#: The methods a manager in each slot needs, from its protocol.
_SLOTS: dict[Slot, tuple[str, ...]] = {
    "cpu": ("after_setup_start", "before_teardown_stop"),
    "memory": ("start", "stop"),
}
#: The ``mew run`` flag that fills each slot.
FLAGS: dict[Slot, str] = {"cpu": "--cpu-profiler", "memory": "--memory-profiler"}
#: Preset keys that name the manager rather than pass an option. ``func``
#: (a function returning the class) is the planned 1.x addition; reserved now
#: so no 1.0 preset can use it as an option name.
_RESERVED_KEYS = ("class", "func")
#: Reserved in flag values for inline options in 1.x.
_RESERVED_CHARS = ",="
_PRESET_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*")


@dataclass(frozen=True, slots=True)
class Preset:
    """A manager to load: which callable, with which keyword arguments.

    Attributes
    ----------
    spec : str
        ``module:attr`` or ``path.py:attr``; a path is relative to the project root.
    options : dict
        Keyword arguments for the callable, in snake_case.
    name : str or None
        The preset's name; ``None`` for a spec given on the CLI.
    """

    spec: str
    options: dict[str, Any] = field(default_factory=dict)
    name: str | None = None

    @property
    def label(self) -> str:
        """How messages name the preset: ``fine`` or ``pkg.mod:Class``."""
        return self.name or self.spec


#: mew's own presets. A built-in may name any callable; a user preset only a
#: class. User presets can't take these names, so a name here marks a built-in.
BUILTINS: dict[str, Preset] = {
    "sample": Preset("mew.cpu:manager", name="sample"),
    "memray": Preset("mew.memory:MemrayManager", name="memray"),
}


class Loaded(NamedTuple):
    """A constructed manager, its slot, the preset it came from, and the class that ran."""

    manager: Any
    slot: Slot
    preset: Preset
    #: The class that ran, as ``module:qualname``, or the spec as written.
    cls: str

    @property
    def record(self) -> dict[str, Any]:
        """How the run's context records the manager."""
        return {"preset": self.preset.name, "class": self.cls, "options": self.preset.options}


def parse_presets(raw: Any) -> dict[str, Preset]:
    """Validate a ``[tool.mew.profilers]`` table into presets.

    Raises
    ------
    ValueError
        If the table, a preset name, or a preset's ``class`` is invalid, a
        preset uses ``func`` (not supported before 1.x), or a preset reuses a
        built-in's name.
    """
    if not isinstance(raw, dict):
        raise ValueError("[tool.mew.profilers] must be a table")  # ruff: ignore[TRY004]
    presets: dict[str, Preset] = {}
    for name, body in raw.items():
        where = f"[tool.mew.profilers.{name}]"
        if not _PRESET_NAME.fullmatch(name):
            raise ValueError(f"{where}: a preset name takes letters, digits, '-' and '_'")
        if name in BUILTINS:
            raise ValueError(f"{where}: {name!r} is a built-in preset")
        if not isinstance(body, dict):
            raise ValueError(f"{where} must be a table")  # ruff: ignore[TRY004]
        if "func" in body:
            raise ValueError(f"{where}: 'func' is reserved\nhint: name a class with 'class'")
        spec = body.get("class")
        if not isinstance(spec, str) or ":" not in spec:
            raise ValueError(f"{where}: 'class' must be a 'module:Class' or 'path.py:Class' string")
        options = {k.replace("-", "_"): v for k, v in body.items() if k not in _RESERVED_KEYS}
        presets[name] = Preset(spec=spec, options=options, name=name)
    return presets


def resolve(value: str, presets: Mapping[str, Preset], *, flag: str) -> Preset:
    """The preset a ``flag`` value names.

    A value with a ``:`` is a spec, run with the class's defaults; any other
    value is a preset name, the user's or mew's own.

    Raises
    ------
    ValueError
        If the value holds a reserved character or names no preset.
    """
    if bad := sorted(set(value) & set(_RESERVED_CHARS)):
        raise ValueError(f"{flag} {value!r}: {' and '.join(map(repr, bad))} are reserved")
    if ":" in value:
        return Preset(spec=value)
    known = BUILTINS | dict(presets)
    if value in known:
        return known[value]
    close = difflib.get_close_matches(value, known, n=1)
    hint = f"did you mean {close[0]!r}?" if close else f"presets are {', '.join(sorted(known))}"
    raise ValueError(f"{flag}: no preset {value!r}\nhint: {hint}")


def _import(preset: Preset, root: Path) -> Any:
    """Import the callable a preset's spec names; a user preset's must be a class."""
    target, _, attr = preset.spec.rpartition(":")
    if not target or not attr:
        raise ValueError(f"{preset.label}: expected 'module:Class' or 'path.py:Class'")
    module = target
    if target.endswith(".py"):
        path = root / target
        if not path.is_file():
            raise ValueError(f"{preset.label}: no file {target} in {root}")
        # Registered in sys.modules under a hashed name, so resolve_name finds it.
        module = _registry.import_file(path).__name__
    try:
        obj = pkgutil.resolve_name(f"{module}:{attr}")
    except AttributeError as e:
        raise ValueError(f"{preset.label}: {target} has no attribute {attr!r}") from e
    if preset.name not in BUILTINS and not isinstance(obj, type):
        raise ValueError(f"{preset.label}: {preset.spec} is a {type(obj).__name__}, not a class")
    return obj


def _check_options(preset: Preset, target: Any) -> None:
    """Reject options the callable takes no keyword for, with a close match if any."""
    try:
        params = inspect.signature(target).parameters.values()
    except (TypeError, ValueError):  # no signature to check, e.g. a C extension type
        return
    if any(p.kind is p.VAR_KEYWORD for p in params):
        return
    accepted = [p.name for p in params if p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)]
    for key in preset.options:
        if key not in accepted:
            # Spelled as in TOML; the parameters are snake_case.
            shown = key.replace("_", "-")
            options = [a.replace("_", "-") for a in accepted]
            close = difflib.get_close_matches(shown, options, n=1)
            hint = f"\nhint: did you mean {close[0]!r}?" if close else ""
            raise ValueError(f"{preset.label}: unknown option {shown!r}{hint}")


def _check_slot(preset: Preset, manager: Any, slot: Slot) -> None:
    """Reject a manager without its slot's methods, naming the flag it fits if any."""

    # getattr_static, as runtime_checkable isinstance does from 3.12: the same
    # verdict on every supported interpreter, and a required method can't come
    # from __getattr__.
    def has(method: str) -> bool:
        return callable(inspect.getattr_static(manager, method, None))

    missing = [m for m in _SLOTS[slot] if not has(m)]
    if not missing:
        return
    name = type(manager).__name__
    kind = "a CPU profiler" if slot == "cpu" else "a memory profiler"
    other: Slot = "memory" if slot == "cpu" else "cpu"
    if all(map(has, _SLOTS[other])):
        raise ValueError(f"{preset.label}: {name} is not {kind}\nhint: pass it to {FLAGS[other]}")
    if len(missing) == 1:  # half a pair: name the other half
        raise ValueError(f"{preset.label}: {name} has no {missing[0]}()")
    first, second = _SLOTS[slot]
    raise ValueError(f"{preset.label}: {name} is not {kind}: define {first}() and {second}()")


def load(preset: Preset, slot: Slot, *, root: Path, stack: ExitStack) -> Loaded:
    """Construct a preset's manager for a slot, and enter it if it is a context manager.

    Parameters
    ----------
    preset : Preset
        What to load, from :func:`resolve`.
    slot : {"cpu", "memory"}
        The slot it fills, whose methods it must have.
    root : Path
        The project root, against which ``path.py:Class`` specs resolve.
    stack : ExitStack
        Holds the manager's context for the run, if it has one.

    Raises
    ------
    ValueError
        If the spec doesn't name a class (a function, for a built-in), an option
        is unknown, missing or of a type the constructor rejects with
        ``TypeError``, or the manager lacks the slot's methods.
    ImportError
        If the spec's module can't be imported; the manager's other errors, such
        as a sampler that can't run here, propagate as they are.
    """
    target = _import(preset, root)
    _check_options(preset, target)
    try:
        manager = target(**preset.options)
    except TypeError as e:
        # A missing option, a bad type for one, or an unknown one that the
        # signature hid: a Protocol subclass without __init__ shows (*args, **kwargs).
        raise ValueError(f"{preset.label}: {e}") from e
    _check_slot(preset, manager, slot)
    if isinstance(manager, AbstractContextManager):
        stack.enter_context(manager)
    # The spec as written where it names the class that ran: a path spec's module
    # is a hashed `mew._bench_*` name. A built-in's function chose the class itself.
    cls = type(manager)
    ran = preset.spec if cls is target else f"{cls.__module__}:{cls.__qualname__}"
    return Loaded(manager, slot, preset, ran)


def load_all(
    values: Mapping[Slot, str | None],
    presets: Mapping[str, Preset],
    *,
    root: Path,
    stack: ExitStack,
) -> dict[Slot, Loaded]:
    """Load the manager each slot's flag names, skipping a slot without a value.

    Raises
    ------
    ValueError
        As :func:`resolve` and :func:`load`.
    ImportError
        As :func:`load`.
    """
    return {
        slot: load(resolve(value, presets, flag=FLAGS[slot]), slot, root=root, stack=stack)
        for slot, value in values.items()
        if value is not None
    }

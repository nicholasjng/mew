"""Loading the managers `mew run --cpu-profiler` and `--memory-profiler` name: presets, specs,
slots."""

from __future__ import annotations

import textwrap
from contextlib import ExitStack
from pathlib import Path

import pytest

from mew import _managers
from mew._managers import Preset

_MANAGERS = '''
import mew

class Mem(mew.MemoryManager):
    """Counts its passes; a context manager, so mew enters it for the run."""

    def __init__(self, peak=1, *, tag="x"):
        self.peak, self.tag, self.entered, self.passes = peak, tag, False, 0

    def __enter__(self):
        self.entered = True
        return None  # mew keeps the instance, not this

    def __exit__(self, *exc):
        self.entered = False

    def start(self):
        pass

    def stop(self):
        self.passes += 1
        return {"peak_bytes": self.peak, "total_allocations": 1}

    def write_outputs(self, directory):
        directory.mkdir(parents=True)
        (directory / "passes.txt").write_text(f"{self.tag}:{self.passes}")

class Prof:
    def after_setup_start(self): ...
    def before_teardown_stop(self): ...

class Both(Mem):
    """Fits either slot."""

    def after_setup_start(self): ...
    def before_teardown_stop(self): ...

class Half:
    def start(self): ...

class Nothing: ...

class Needs(mew.MemoryManager):
    def __init__(self, peak):
        self.peak = peak

    def start(self): ...
    def stop(self): ...

class Bare(mew.MemoryManager):
    """No __init__: the Protocol's stands in, with a (*args, **kwargs) signature."""

    def start(self): ...
    def stop(self): ...

def not_a_class(): ...
'''


@pytest.fixture
def root(tmp_path: Path) -> Path:
    (tmp_path / "managers.py").write_text(textwrap.dedent(_MANAGERS))
    return tmp_path


def _load(value: str, root: Path, presets: dict[str, Preset] | None = None, *, slot="memory"):
    with ExitStack() as stack:
        (loaded,) = _managers.load_all(
            {slot: value}, presets or {}, root=root, stack=stack
        ).values()
    return loaded


def test_parse_presets_turns_kebab_keys_into_keyword_arguments():
    presets = _managers.parse_presets({"fine": {"class": "m:C", "top-k": 20, "interval": 1e-3}})
    assert presets == {"fine": Preset("m:C", {"top_k": 20, "interval": 1e-3}, name="fine")}


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ({"sample": {"class": "m:C"}}, "'sample' is a built-in preset"),
        ({"x": {"func": "m:f"}}, "'func' is reserved"),
        ({"x": {"interval": 1}}, "'class' must be"),
        ({"x y": {"class": "m:C"}}, "a preset name takes"),
        ({"x": "m:C"}, "must be a table"),
    ],
)
def test_parse_presets_rejects(raw, message):
    with pytest.raises(ValueError, match=message):
        _managers.parse_presets(raw)


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("mew.cpu:manager,interval=1", "--cpu-profiler .*: ',' and '=' are reserved"),
        ("smaple", "did you mean 'sample'"),
        ("zzz", "presets: memray, sample"),
    ],
)
def test_resolve_rejects(value, message):
    with pytest.raises(ValueError, match=message):
        _managers.resolve(value, {}, flag="--cpu-profiler")


def test_preset_constructs_the_class_with_its_options_and_enters_it(root):
    loaded = _load("mine", root, {"mine": Preset("managers.py:Mem", {"peak": 7}, name="mine")})
    assert loaded.slot == "memory"
    assert loaded.manager.peak == 7
    # Entered for the run, exited with the stack.
    assert loaded.manager.entered is False
    # A path spec as written: its module is a hashed internal name.
    assert loaded.record == {"preset": "mine", "class": "managers.py:Mem", "options": {"peak": 7}}


def test_each_flag_fills_its_slot(root):
    """A run takes one manager per slot, each named by its flag; a slot without one stays empty.
    A class with both slots' methods fits either."""
    values = {"cpu": "managers.py:Both", "memory": "managers.py:Mem"}
    with ExitStack() as stack:
        loaded = _managers.load_all(values, {}, root=root, stack=stack)
        assert _managers.load_all({"cpu": None, "memory": None}, {}, root=root, stack=stack) == {}
    assert {slot: type(m.manager).__name__ for slot, m in loaded.items()} == {
        "cpu": "Both",
        "memory": "Mem",
    }


def test_builtin_records_the_class_its_function_chose(root):
    loaded = _load("sample", root, slot="cpu")
    assert loaded.slot == "cpu"
    assert loaded.record["class"] in ("mew.cpu:TachyonManager", "mew.cpu:PyinstrumentManager")


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("managers.py:not_a_class", "is a function, not a class"),
        ("managers.py:Missing", "has no attribute 'Missing'"),
        ("nowhere.py:Mem", "no file nowhere.py"),
        ("managers.py:Prof", "Prof is not a memory profiler; pass it to --cpu-profiler"),
        ("managers.py:Half", "Half has no stop()"),
        ("managers.py:Nothing", r"Nothing is not a memory profiler: define start\(\) and stop\(\)"),
        (
            "managers.py:Needs",
            "managers.py:Needs: .*missing 1 required positional argument: 'peak'",
        ),
    ],
)
def test_load_rejects(root, value, message):
    with pytest.raises(ValueError, match=message):
        _load(value, root)


def test_unknown_option_suggests_the_close_one(root):
    with pytest.raises(ValueError, match="unknown option 'peek'; did you mean 'peak'"):
        _load("m", root, {"m": Preset("managers.py:Mem", {"peek": 1}, name="m")})


def test_option_for_a_class_without_init_fails_as_a_value_error(root):
    # No signature to check the option against, so construction rejects it.
    with pytest.raises(ValueError, match="^m: Bare"):
        _load("m", root, {"m": Preset("managers.py:Bare", {"bogus": 1}, name="m")})


def test_a_memory_profiler_named_for_the_cpu_slot_points_to_its_flag(root):
    with pytest.raises(ValueError, match="Mem is not a CPU profiler; pass it to --memory-profiler"):
        _load("managers.py:Mem", root, slot="cpu")

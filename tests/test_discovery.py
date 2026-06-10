"""File discovery and module import side-effects."""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest

import mew.discovery as discovery
from mew._registry import REGISTRY


@pytest.fixture
def _restore_sys_path():
    saved = list(sys.path)
    saved_modules = set(sys.modules)
    yield
    sys.path[:] = saved
    for name in set(sys.modules) - saved_modules:
        sys.modules.pop(name, None)


def _write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(body))


def test_parse_plain_path():
    s = discovery.parse("benchmarks/bench_sort.py")
    assert s.path == Path("benchmarks/bench_sort.py")
    assert s.filter is None


def test_parse_nodeid():
    s = discovery.parse("benchmarks/bench_sort.py::quicksort")
    assert s.path == Path("benchmarks/bench_sort.py")
    assert s.filter == "quicksort"


def test_collect_files_globs_directory(tmp_path):
    _write(tmp_path / "bench_a.py", "")
    _write(tmp_path / "nested" / "bench_b.py", "")
    _write(tmp_path / "ignore_me.py", "")
    files = discovery.collect_files(
        [discovery.Selector(tmp_path)],
        file_patterns=["bench_*.py"],
    )
    assert {f.name for f in files} == {"bench_a.py", "bench_b.py"}


def test_collect_files_accepts_single_file(tmp_path):
    f = tmp_path / "some_bench.py"
    _write(f, "")
    files = discovery.collect_files(
        [discovery.Selector(f)],
        file_patterns=["bench_*.py"],
    )
    assert files == [f.resolve()]


def test_import_file_populates_registry(tmp_path):
    bench = tmp_path / "bench_demo.py"
    _write(
        bench,
        """
        import mew

        @mew.benchmark
        def bench_x(state):
            for _ in state:
                pass
    """,
    )
    discovery.import_file(bench)
    names = [e.name for e in REGISTRY.all()]
    assert len(names) == 1
    assert "bench_x" in names[0]


def test_import_file_allows_sibling_imports(tmp_path, _restore_sys_path):
    _write(tmp_path / "_bench_fixtures.py", "VALUE = 123\n")
    bench = tmp_path / "bench_uses_fixture.py"
    _write(
        bench,
        """
        import mew
        from _bench_fixtures import VALUE

        assert VALUE == 123

        @mew.benchmark
        def bench_x(state):
            for _ in state:
                pass
    """,
    )
    # Must not raise ModuleNotFoundError on the sibling import.
    discovery.import_file(bench)
    assert any("bench_x" in e.name for e in REGISTRY.all())


def test_discovered_unloads_only_what_it_added(tmp_path, _restore_sys_path):
    _write(tmp_path / "_bench_fixtures.py", "VALUE = 7\n")
    bench = tmp_path / "bench_scoped.py"
    _write(
        bench,
        """
        import mew
        from _bench_fixtures import VALUE

        @mew.benchmark
        def bench_x(state):
            for _ in state:
                pass
    """,
    )
    parent = str(tmp_path.resolve())
    mod_name = f"mew._bench_{abs(hash(bench.resolve()))}"

    with discovery.discovered():
        discovery.import_file(bench)
        entry = next(e for e in REGISTRY.all() if "bench_x" in e.name)
        assert mod_name in sys.modules  # present while the block is open
        assert parent in sys.path

    # Boundary cleanup: our synthetic module and path insert are gone...
    assert mod_name not in sys.modules
    assert parent not in sys.path
    # ...but the sibling module is left untouched (no risky sys.modules pruning).
    assert "_bench_fixtures" in sys.modules
    # ...and the registered function still works: its module namespace survives
    # via __globals__ even though sys.modules no longer holds the module.
    assert entry.fn.__globals__["VALUE"] == 7  # ty: ignore[unresolved-attribute]


def test_unload_is_idempotent():
    # Nothing tracked → no error, no effect.
    discovery.unload()
    discovery.unload()


def test_collect_files_missing_path_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        discovery.collect_files(
            [discovery.Selector(tmp_path / "nope")],
            file_patterns=["bench_*.py"],
        )

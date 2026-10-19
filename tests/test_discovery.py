"""File discovery and module import side-effects."""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest

from mew import _discovery as discovery
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


@pytest.mark.parametrize(
    ("raw", "expected_filter"),
    [
        ("benchmarks/bench_sort.py", None),
        ("benchmarks/bench_sort.py::quicksort", "quicksort"),
    ],
)
def test_parse(tmp_path, raw, expected_filter):
    s = discovery.parse(raw, root=tmp_path)
    assert s.path == tmp_path.resolve() / "benchmarks" / "bench_sort.py"
    assert (s.pattern.pattern if s.pattern else None) == expected_filter


def test_collect_files_globs_directory(tmp_path):
    _write(tmp_path / "bench_a.py", "")
    _write(tmp_path / "nested" / "bench_b.py", "")
    _write(tmp_path / "ignore_me.py", "")
    files = discovery.collect_files(
        [discovery.parse(str(tmp_path))],
        file_patterns=["bench_*.py"],
    )
    assert {f.name for f in files} == {"bench_a.py", "bench_b.py"}


def test_collect_files_accepts_single_file(tmp_path):
    f = tmp_path / "some_bench.py"
    _write(f, "")
    files = discovery.collect_files(
        [discovery.parse(str(f))],
        file_patterns=["bench_*.py"],
    )
    assert files == [f.resolve()]


def test_discovered_unloads_only_what_it_added(tmp_path, _restore_sys_path):
    _write(tmp_path / "_bench_fixtures.py", "VALUE = 7\n")
    _write(
        tmp_path / "bench_scoped.py",
        """
        import mew
        from _bench_fixtures import VALUE  # a sibling, importable from the file's directory

        @mew.benchmark
        def bench_x(state):
            pass
        """,
    )
    parent = str(tmp_path.resolve())

    with discovery.discovered():
        discovery.import_file(tmp_path / "bench_scoped.py")
        (entry,) = REGISTRY.all()
        module = entry.fn.__module__
        assert module in sys.modules and parent in sys.path

    assert module not in sys.modules and parent not in sys.path
    # Sibling modules stay: pruning sys.modules wholesale is risky.
    assert "_bench_fixtures" in sys.modules


def test_collect_files_missing_path_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        discovery.collect_files(
            [discovery.parse(str(tmp_path / "nope"))],
            file_patterns=["bench_*.py"],
        )


@pytest.mark.parametrize(
    ("stdin_name", "expected_cases"),
    [(None, None), ("f[n=10]", [1])],
)
def test_select_entries_is_independent_of_discovery(tmp_path, stdin_name, expected_cases):
    from mew._registry import Entry, compile_name_filter

    # These paths do not exist: selection uses already-resolved lexical paths.
    source = tmp_path / "suite" / "bench_example.py"
    entry = Entry("f", lambda s: None, case_labels=["n=1", "n=10"])
    selector = discovery.Selector(source.parent, True, None)
    names = [compile_name_filter(stdin_name, literal=True)] if stdin_name else []
    selected = discovery.select_entries([(entry, source)], [selector], names=names)
    assert len(selected) == 1
    assert selected[0].cases == expected_cases
    assert entry.cases is None


def test_select_entries_scopes_path_filters_to_their_paths(tmp_path):
    from mew._registry import Entry, compile_name_filter

    a, b = tmp_path / "a.py", tmp_path / "b.py"
    entries = [(Entry(name, lambda s: None), path) for path in (a, b) for name in ("f", "g")]
    selectors = [
        discovery.Selector(a, False, compile_name_filter("f")),
        discovery.Selector(b, False, compile_name_filter("g")),
    ]
    assert discovery.select_entries(entries, selectors) == [entries[0][0], entries[3][0]]

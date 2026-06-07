"""File discovery and module import side-effects."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

import mew.discovery as discovery
from mew._registry import REGISTRY


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


def test_collect_files_missing_path_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        discovery.collect_files(
            [discovery.Selector(tmp_path / "nope")],
            file_patterns=["bench_*.py"],
        )

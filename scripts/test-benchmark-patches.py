"""Build native patch regressions against HEAD of a local benchmark checkout.

Usage: python scripts/test-benchmark-patches.py /path/to/google/benchmark
The input checkout is read only; its local edits are excluded from the build.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import tempfile
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("benchmark_source", type=Path)
    parser.add_argument("--build-type", choices=["Debug", "Release"], default="Debug")
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[1]
    git = shutil.which("git")
    if git is None:
        parser.error("git is required")
    with tempfile.TemporaryDirectory(prefix="mew-patch-tests-") as work:
        source = Path(work) / "source"
        build = Path(work) / "build"
        subprocess.run(
            [
                git,
                "clone",
                "--shared",
                "--quiet",
                str(args.benchmark_source.resolve()),
                str(source),
            ],
            check=True,
        )
        # Exercise the same application path as the package build, including
        # its three-way fallback and stamp-based idempotence.
        patches = "|".join(str(patch) for patch in sorted((repo / "patches").glob("*.patch")))
        apply = [
            "cmake",
            f"-DGIT={git}",
            f"-DSRC={source}",
            f"-DPATCHES={patches}",
            "-P",
            str(repo / "cmake/apply_patches.cmake"),
        ]
        subprocess.run(apply, check=True)
        subprocess.run(apply, check=True)
        subprocess.run(
            [
                "cmake",
                "-S",
                str(repo / "tests/native"),
                "-B",
                str(build),
                f"-DBENCHMARK_SOURCE={source}",
                f"-DCMAKE_BUILD_TYPE={args.build_type}",
            ],
            check=True,
        )
        subprocess.run(["cmake", "--build", str(build), "--parallel", "4"], check=True)
        subprocess.run(["ctest", "--test-dir", str(build), "--output-on-failure"], check=True)


if __name__ == "__main__":
    main()

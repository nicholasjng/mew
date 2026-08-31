#!/usr/bin/env python3
"""Configure the stable CMake build tree used by clangd."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILD_DIR = ROOT / "build" / "clangd"


def main() -> None:
    if importlib.util.find_spec("nanobind") is None:
        raise SystemExit("nanobind is missing; run: uv sync --group build")

    subprocess.run(
        [
            "cmake",
            "-S",
            str(ROOT),
            "-B",
            str(BUILD_DIR),
            f"-DPython_EXECUTABLE={sys.executable}",
            f"-DFETCHCONTENT_BASE_DIR={ROOT / 'build' / '_deps'}",
            "-DMEW_SYNC_COMPILE_COMMANDS=ON",
        ],
        check=True,
    )

    database = ROOT / "compile_commands.json"
    if database.is_symlink():
        database.unlink()
    subprocess.run(
        ["cmake", "--build", str(BUILD_DIR), "--target", "sync_compile_commands"],
        check=True,
    )


if __name__ == "__main__":
    main()

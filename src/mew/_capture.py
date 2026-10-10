"""Capture what benchmarked code writes to stdout and stderr during a run.

Native libraries write to file descriptors 1 and 2 directly, so the capture
redirects the descriptors themselves into one file. The bindings suspend it
around every reporter callback, so reporters write to the real streams, and the
captured output is printed to stderr once the run is over.
"""

from __future__ import annotations

import os
import sys
from typing import BinaryIO


class OutputCapture:
    """Redirect fds 1 and 2 into ``file`` until :meth:`close`; the caller owns ``file``."""

    def __init__(self, file: BinaryIO) -> None:
        self._file = file
        self._saved = (os.dup(1), os.dup(2))
        self._active = False
        self.resume()

    def resume(self) -> None:
        sys.stdout.flush()
        sys.stderr.flush()
        os.dup2(self._file.fileno(), 1)
        os.dup2(self._file.fileno(), 2)
        self._active = True

    def suspend(self) -> None:
        # Flush buffered Python writes into the capture before switching back.
        sys.stdout.flush()
        sys.stderr.flush()
        os.dup2(self._saved[0], 1)
        os.dup2(self._saved[1], 2)
        self._active = False

    def close(self) -> None:
        """Restore the streams, then print everything captured to stderr."""
        if self._active:
            self.suspend()
        for fd in self._saved:
            os.close(fd)
        self._file.seek(0)
        if captured := self._file.read().decode(errors="replace"):
            sys.stderr.write(f"\noutput from benchmarks:\n{captured}")
            sys.stderr.flush()

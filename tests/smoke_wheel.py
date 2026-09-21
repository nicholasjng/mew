"""Assert an installed wheel is usable, for cibuildwheel's test step.

Not a pytest module: it runs against the *installed* package from outside the
source tree, checking that the repaired extension loads and that the declared
package data shipped. Behaviour is covered by the regular suite.
"""

import sys
import sysconfig
from importlib import resources
from pathlib import Path

import mew
from mew import _core

# Not the source tree: a stray `src/mew` on sys.path would hide a broken wheel.
here = Path(mew.__file__).resolve()
assert "site-packages" in here.parts, f"mew imported from {here}, not an install"

# Loading _core at all proves the repaired extension still resolves its
# libraries; a botched RPATH rewrite fails right here.
assert _core.BENCHMARK_VERSION, "Google Benchmark version missing from _core"
assert mew.__version__, "mew.__version__ is empty"  # ty: ignore[redundant-condition]

# Declared package data, absent from the wheel if wheel.packages ever drifts.
for data in ("py.typed", "_core.pyi"):
    assert (resources.files("mew") / data).is_file(), f"{data} missing from wheel"

# On a free-threaded build, `import mew._core` above would have made CPython
# re-enable the GIL had the wheel been built without Py_MOD_GIL_NOT_USED.
if sysconfig.get_config_var("Py_GIL_DISABLED"):
    # getattr: the probe only exists on 3.13+, below mew's 3.11 floor.
    gil_enabled = getattr(sys, "_is_gil_enabled", lambda: True)()
    assert not gil_enabled, "importing mew._core re-enabled the GIL"

print(f"ok: mew {mew.__version__}, Google Benchmark {_core.BENCHMARK_VERSION}, {here.name}")

#!/usr/bin/env bash
#
# Run pytest under AddressSanitizer on macOS.
#
# Prerequisites:
#   - Activate the uv-managed venv (VIRTUAL_ENV set).
#   - Build the ASAN editable install:
#         DUCKY_ASAN=1 uv sync --all-groups --reinstall-package=ducky
#
# Usage: scripts/asan-pytest.sh [pytest args]

set -euo pipefail

if [[ -z "${VIRTUAL_ENV:-}" ]]; then
  echo "scripts/asan-pytest.sh: VIRTUAL_ENV is unset — activate the venv first" >&2
  exit 1
fi
VENV_PY="$VIRTUAL_ENV/bin/python"
if [[ ! -x "$VENV_PY" ]]; then
  echo "scripts/asan-pytest.sh: $VENV_PY not found — is VIRTUAL_ENV correct?" >&2
  exit 1
fi

# whoami.py, taken from https://jonasdevlieghere.com/post/sanitizing-python-modules/.
DEEP_PY=$("$VENV_PY" - <<'PY'
import ctypes
dyld = ctypes.cdll.LoadLibrary('/usr/lib/system/libdyld.dylib')
n = ctypes.c_ulong(1024)
buf = ctypes.create_string_buffer(b'\000', n.value)
dyld._NSGetExecutablePath(ctypes.byref(buf), ctypes.byref(n))
print(buf.value.decode())
PY
)

DYLD_INSERT_LIBRARIES="$(clang -print-file-name=libclang_rt.asan_osx_dynamic.dylib)" \
ASAN_OPTIONS="${ASAN_OPTIONS:-detect_leaks=0:halt_on_error=1}" \
__PYVENV_LAUNCHER__="$VENV_PY" \
  "$DEEP_PY" -m pytest --capture no "$@"

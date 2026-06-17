#!/usr/bin/env bash
#
# Run pytest under ThreadSanitizer on macOS.
#
# Prerequisites:
#   - Activate the uv-managed venv (VIRTUAL_ENV set). For race detection on
#     threaded benchmarks, use a free-threaded interpreter (e.g. .venv-ft):
#         UV_PROJECT_ENVIRONMENT=.venv-ft MEW_TSAN=1 \
#             uv sync --python 3.13t --all-groups --reinstall-package=mew
#   - Otherwise build the TSAN editable install into the active venv:
#         MEW_TSAN=1 uv sync --all-groups --reinstall-package=mew
#
# Usage: scripts/tsan-pytest.sh [pytest args]

set -euo pipefail

if [[ -z "${VIRTUAL_ENV:-}" ]]; then
  echo "scripts/tsan-pytest.sh: VIRTUAL_ENV is unset — activate the venv first" >&2
  exit 1
fi
VENV_PY="$VIRTUAL_ENV/bin/python"
if [[ ! -x "$VENV_PY" ]]; then
  echo "scripts/tsan-pytest.sh: $VENV_PY not found — is VIRTUAL_ENV correct?" >&2
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

DYLD_INSERT_LIBRARIES="$(clang -print-file-name=libclang_rt.tsan_osx_dynamic.dylib)" \
TSAN_OPTIONS="${TSAN_OPTIONS:-halt_on_error=1}" \
__PYVENV_LAUNCHER__="$VENV_PY" \
  "$DEEP_PY" -m pytest --capture no "$@"

#!/usr/bin/env bash
#
# Run pytest under AddressSanitizer or ThreadSanitizer on macOS. SIP strips
# DYLD_INSERT_LIBRARIES when a protected binary, such as a system Python
# launcher, runs in between, so this starts the interpreter behind the venv's
# python directly.
#
# Prerequisites: activate the uv-managed venv (VIRTUAL_ENV set) holding the
# sanitizer's editable install:
#     MEW_ASAN=1 uv sync --group test --group duckdb --reinstall-package=mew-bench
#     MEW_TSAN=1 uv sync --group test --reinstall-package=mew-bench
# For races in threaded benchmarks, build TSAN into a free-threaded venv:
#     UV_PROJECT_ENVIRONMENT=.venv-ft MEW_TSAN=1 \
#         uv sync --python 3.14t --group test --reinstall-package=mew-bench
#
# Usage: scripts/sanitizer-pytest.sh asan|tsan [pytest args]

set -euo pipefail

me="scripts/sanitizer-pytest.sh"
case "${1:-}" in
  asan) options_var=ASAN_OPTIONS options="detect_leaks=0:halt_on_error=1" ;;
  tsan) options_var=TSAN_OPTIONS options="halt_on_error=1" ;;
  *)
    echo "usage: $me asan|tsan [pytest args]" >&2
    exit 2
    ;;
esac
sanitizer=$1
shift

if [[ -z "${VIRTUAL_ENV:-}" ]]; then
  echo "$me: VIRTUAL_ENV is unset — activate the venv first" >&2
  exit 1
fi
VENV_PY="$VIRTUAL_ENV/bin/python"
if [[ ! -x "$VENV_PY" ]]; then
  echo "$me: $VENV_PY not found — is VIRTUAL_ENV correct?" >&2
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

# At startup the runtime strips itself from DYLD_INSERT_LIBRARIES, which would
# leave child interpreters, such as the CLI tests' `python -m mew.cli`, without
# it; strip_env=0 keeps it. The caller's own options replace the defaults above
# and, coming later, can turn strip_env back on.
export "$options_var=strip_env=0:${!options_var:-$options}"
DYLD_INSERT_LIBRARIES="$(clang -print-file-name="libclang_rt.${sanitizer}_osx_dynamic.dylib")" \
__PYVENV_LAUNCHER__="$VENV_PY" \
  "$DEEP_PY" -m pytest --capture no "$@"

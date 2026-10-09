# Apply mew's Google Benchmark patches to a FetchContent-populated source tree.
#
# Run via `cmake -P` from PATCH_COMMAND with -DGIT=, -DSRC= and -DPATCHES= (a
# |-separated list). PATCHES_SHA256 is unused; it only makes FetchContent re-run
# this script when a patch changes.
#
# A stamp file records the base commit and applied series. Patches build on each
# other, so a stale or missing stamp resets the tree and re-applies them all.

if(NOT GIT OR NOT SRC OR NOT PATCHES)
    message(FATAL_ERROR "apply_patches.cmake: GIT, SRC and PATCHES are all required")
endif()
string(REPLACE "|" ";" PATCHES "${PATCHES}")

set(_stamp "${SRC}/.mew-patches-applied")
# `git apply` leaves HEAD alone, so this is the pinned commit even when patched.
execute_process(
    COMMAND "${GIT}" -C "${SRC}" rev-parse HEAD
    OUTPUT_VARIABLE _base
    OUTPUT_STRIP_TRAILING_WHITESPACE
    ERROR_QUIET)
set(_series "base ${_base}\n")
foreach(patch IN LISTS PATCHES)
    if(NOT EXISTS "${patch}")
        message(FATAL_ERROR "apply_patches.cmake: no such patch: ${patch}")
    endif()
    get_filename_component(_name "${patch}" NAME)
    file(SHA256 "${patch}" _sha)
    string(APPEND _series "${_name} ${_sha}\n")
endforeach()

if(EXISTS "${_stamp}")
    file(READ "${_stamp}" _applied)
    if(_applied STREQUAL _series)
        message(STATUS "Google Benchmark patches already applied")
        return()
    endif()
    message(STATUS "Google Benchmark patch series changed; re-applying")
    file(REMOVE "${_stamp}")
endif()

# Safe: the tree is FetchContent's own clone, only ever modified by this script.
execute_process(
    COMMAND "${GIT}" -C "${SRC}" reset --hard --quiet
    RESULT_VARIABLE _rc
    ERROR_VARIABLE _err)
if(NOT _rc EQUAL 0)
    message(FATAL_ERROR "apply_patches.cmake: git reset failed in ${SRC}\n${_err}")
endif()

foreach(patch IN LISTS PATCHES)
    get_filename_component(_name "${patch}" NAME)

    # --3way uses the pre-image blobs a FetchContent clone has; it degrades to a
    # normal apply otherwise.
    execute_process(
        COMMAND "${GIT}" -C "${SRC}" apply --3way --whitespace=nowarn "${patch}"
        RESULT_VARIABLE _rc
        ERROR_VARIABLE _err)

    # --3way cannot absorb a CRLF/LF mismatch and doesn't combine with
    # --ignore-whitespace, hence the fallback. Both sides should be LF anyway.
    if(NOT _rc EQUAL 0)
        execute_process(
            COMMAND "${GIT}" -C "${SRC}" apply --ignore-whitespace "${patch}"
            RESULT_VARIABLE _rc
            ERROR_QUIET)
        if(_rc EQUAL 0)
            message(WARNING
                "Google Benchmark patch ${_name} only applied after ignoring "
                "whitespace; the checkout's line endings likely differ from the "
                "patch (expected LF on both sides).")
        endif()
    endif()

    if(NOT _rc EQUAL 0)
        message(FATAL_ERROR
            "Google Benchmark patch failed to apply: ${_name}\n"
            "The pinned commit has probably moved out from under it; rebase the "
            "patch against the new pin.\n"
            "${_err}")
    endif()
    message(STATUS "Google Benchmark patch applied: ${_name}")
endforeach()

file(WRITE "${_stamp}" "${_series}")

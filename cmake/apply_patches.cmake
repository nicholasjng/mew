# Apply mew's Google Benchmark patches to a FetchContent-populated source tree.
#
# Driven as a `cmake -P` script from FetchContent's PATCH_COMMAND, which has no
# shell. Expects -DGIT=, -DSRC= and -DPATCHES= (a |-separated list: a ;-list
# would be split into separate arguments on the PATCH_COMMAND line).
#
# Idempotent through a stamp file recording the applied series. Patches build
# on each other, so a per-patch "already applied" probe cannot work; instead a
# stale or missing stamp resets the tree to the pinned commit and re-applies
# the whole series in order.

if(NOT GIT OR NOT SRC OR NOT PATCHES)
    message(FATAL_ERROR "apply_patches.cmake: GIT, SRC and PATCHES are all required")
endif()
string(REPLACE "|" ";" PATCHES "${PATCHES}")

set(_stamp "${SRC}/.mew-patches-applied")
set(_series "")
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

# Back to the pinned commit. The tree is FetchContent's own clone, so the only
# local modifications are a previous run of this script.
execute_process(
    COMMAND "${GIT}" -C "${SRC}" reset --hard --quiet
    RESULT_VARIABLE _rc
    ERROR_VARIABLE _err)
if(NOT _rc EQUAL 0)
    message(FATAL_ERROR "apply_patches.cmake: git reset failed in ${SRC}\n${_err}")
endif()

foreach(patch IN LISTS PATCHES)
    get_filename_component(_name "${patch}" NAME)

    # --3way needs the pre-image blobs in the object store, which a
    # FetchContent git checkout has. It degrades to a normal apply otherwise.
    execute_process(
        COMMAND "${GIT}" -C "${SRC}" apply --3way --whitespace=nowarn "${patch}"
        RESULT_VARIABLE _rc
        ERROR_VARIABLE _err)

    # `--3way` merges blobs rather than matching text, so it cannot absorb a
    # CRLF/LF difference between the patch and the tree; `--ignore-whitespace`
    # can, but the two flags do not combine. `.gitattributes` and the GIT_CONFIG
    # on the FetchContent_Declare should keep both sides at LF.
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

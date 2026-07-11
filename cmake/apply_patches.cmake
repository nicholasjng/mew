# Apply mew's Google Benchmark patches to a FetchContent-populated source tree.
#
# Driven as a `cmake -P` script from FetchContent's PATCH_COMMAND, which has no
# shell: the idempotence guard and the error handling need real script logic,
# not `||`.
#
# Expects -DGIT=, -DSRC= and a ;-list -DPATCHES=.

if(NOT GIT OR NOT SRC OR NOT PATCHES)
    message(FATAL_ERROR "apply_patches.cmake: GIT, SRC and PATCHES are all required")
endif()

foreach(patch IN LISTS PATCHES)
    if(NOT EXISTS "${patch}")
        message(FATAL_ERROR "apply_patches.cmake: no such patch: ${patch}")
    endif()
    get_filename_component(_name "${patch}" NAME)

    # A patch that reverse-applies cleanly is already in the tree. FetchContent
    # runs PATCH_COMMAND only on (re-)population, but a manual re-run or a
    # restored `_deps` cache must not fail the configure.
    execute_process(
        COMMAND "${GIT}" -C "${SRC}" apply --reverse --check "${patch}"
        RESULT_VARIABLE _already
        OUTPUT_QUIET ERROR_QUIET)
    if(_already EQUAL 0)
        message(STATUS "Google Benchmark patch already applied: ${_name}")
        continue()
    endif()

    # --3way needs the pre-image blobs in the object store, which a
    # FetchContent git checkout has. It degrades to a normal apply otherwise.
    execute_process(
        COMMAND "${GIT}" -C "${SRC}" apply --3way --whitespace=nowarn "${patch}"
        RESULT_VARIABLE _rc
        ERROR_VARIABLE _err)
    if(NOT _rc EQUAL 0)
        message(FATAL_ERROR
            "Google Benchmark patch failed to apply: ${_name}\n"
            "The pinned commit has probably moved out from under it; rebase the "
            "patch against the new pin (see notes/advancing-google-benchmark.md).\n"
            "${_err}")
    endif()
    message(STATUS "Google Benchmark patch applied: ${_name}")
endforeach()

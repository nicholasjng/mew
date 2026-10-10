cmake_minimum_required(VERSION 3.21)
project(mew LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)

# Copying to the source root is opt-in so isolated package builds don't replace
# it with paths from a temporary environment.
set(CMAKE_EXPORT_COMPILE_COMMANDS ON CACHE INTERNAL "")
option(MEW_SYNC_COMPILE_COMMANDS
    "Copy compile_commands.json to the source root" OFF)
if(PROJECT_IS_TOP_LEVEL AND MEW_SYNC_COMPILE_COMMANDS)
    add_custom_target(
        sync_compile_commands ALL
        COMMAND ${CMAKE_COMMAND} -E copy_if_different
            ${CMAKE_BINARY_DIR}/compile_commands.json
            ${CMAKE_CURRENT_SOURCE_DIR}/compile_commands.json
        BYPRODUCTS ${CMAKE_CURRENT_SOURCE_DIR}/compile_commands.json
        COMMENT "Syncing compile_commands.json to source root"
        VERBATIM)
endif()

if(NOT CMAKE_BUILD_TYPE)
    set(CMAKE_BUILD_TYPE Release)
endif()

# AddressSanitizer, enabled with -DMEW_ASAN=ON or MEW_ASAN=1. Global flags also
# instrument Google Benchmark; _core repeats them for nanobind's stub generator.
option(MEW_ASAN "Build with AddressSanitizer" OFF)
if(NOT MEW_ASAN AND DEFINED ENV{MEW_ASAN} AND "$ENV{MEW_ASAN}")
    set(MEW_ASAN ON)
endif()
if(MEW_ASAN)
    add_compile_options(-fsanitize=address -fno-omit-frame-pointer)
    add_link_options(-fsanitize=address)
endif()

# ThreadSanitizer, enabled with -DMEW_TSAN=ON or MEW_TSAN=1. Mutually exclusive
# with ASAN: the two runtimes can't co-load.
option(MEW_TSAN "Build with ThreadSanitizer" OFF)
if(NOT MEW_TSAN AND DEFINED ENV{MEW_TSAN} AND "$ENV{MEW_TSAN}")
    set(MEW_TSAN ON)
endif()
if(MEW_TSAN AND MEW_ASAN)
    message(FATAL_ERROR "MEW_TSAN and MEW_ASAN cannot be enabled at the same time")
endif()
if(MEW_TSAN)
    add_compile_options(-fsanitize=thread -fno-omit-frame-pointer)
    add_link_options(-fsanitize=thread)
endif()

# --- Python + nanobind --------------------------------------------------------
# SABI is optional so a missing module degrades to a version-specific build, also
# outside scikit-build-core where SKBUILD_SABI_COMPONENT is undefined.
find_package(Python 3.11
    REQUIRED COMPONENTS Interpreter Development.Module
    OPTIONAL_COMPONENTS Development.SABIModule)

if(SKBUILD_SABI_COMPONENT AND NOT TARGET Python::SABIModule)
    message(FATAL_ERROR
        "scikit-build-core is building a stable-ABI wheel, but Python::SABIModule "
        "was not found. Install the Python SABI development files, or clear "
        "tool.scikit-build.wheel.py-api to build an untagged wheel.")
endif()

execute_process(
    COMMAND "${Python_EXECUTABLE}" -m nanobind --cmake_dir
    OUTPUT_STRIP_TRAILING_WHITESPACE
    OUTPUT_VARIABLE nanobind_ROOT
)
find_package(nanobind CONFIG REQUIRED)

# --- Google Benchmark ---------------------------------------------------------
include(FetchContent)

set(_mew_benchmark_default_commit "e662de9aab8e705ecf4fa4bd41a207e5a0acfd0c")

# A plain CACHE default would keep an existing build tree on the old pin after
# a bump. Follow the default unless the cached value was overridden, i.e. it
# differs from the default this tree was last configured with.
if(NOT DEFINED MEW_BENCHMARK_COMMIT
   OR MEW_BENCHMARK_COMMIT STREQUAL "${_MEW_BENCHMARK_LAST_DEFAULT_COMMIT}")
    set(MEW_BENCHMARK_COMMIT "${_mew_benchmark_default_commit}"
        CACHE STRING "Google Benchmark commit SHA to fetch" FORCE)
endif()
set(_MEW_BENCHMARK_LAST_DEFAULT_COMMIT "${_mew_benchmark_default_commit}"
    CACHE INTERNAL "")

set(BENCHMARK_ENABLE_TESTING OFF CACHE BOOL "" FORCE)
set(BENCHMARK_ENABLE_GTEST_TESTS OFF CACHE BOOL "" FORCE)
set(BENCHMARK_ENABLE_INSTALL OFF CACHE BOOL "" FORCE)
set(BENCHMARK_ENABLE_WERROR OFF CACHE BOOL "" FORCE)

# Applied in order; later patches build on earlier ones.
set(MEW_BENCHMARK_PATCHES
    "${CMAKE_CURRENT_SOURCE_DIR}/patches/0001-profiler-stop-on-early-exit.patch"
    "${CMAKE_CURRENT_SOURCE_DIR}/patches/0002-profilermanager-result.patch"
    "${CMAKE_CURRENT_SOURCE_DIR}/patches/0003-memory-pass-iterations.patch"
    "${CMAKE_CURRENT_SOURCE_DIR}/patches/0004-memory-iterations-flag.patch"
    "${CMAKE_CURRENT_SOURCE_DIR}/patches/0005-skip-passes-after-failure.patch"
    "${CMAKE_CURRENT_SOURCE_DIR}/patches/0006-timer-running.patch"
    "${CMAKE_CURRENT_SOURCE_DIR}/patches/0007-manager-pass-thread-count.patch")

string(JOIN "|" _mew_patches_arg ${MEW_BENCHMARK_PATCHES})

# FetchContent re-runs the patch step only when its command changes, so the
# command carries a hash of the patches (recomputed on configure).
set(_mew_patches_hash "")
foreach(patch IN LISTS MEW_BENCHMARK_PATCHES)
    file(SHA256 "${patch}" _mew_patch_sha)
    string(APPEND _mew_patches_hash "${_mew_patch_sha}")
endforeach()
string(SHA256 _mew_patches_hash "${_mew_patches_hash}")
set_property(DIRECTORY APPEND PROPERTY CMAKE_CONFIGURE_DEPENDS ${MEW_BENCHMARK_PATCHES})

# PATCH_COMMAND needs git, so resolve it before the Declare.
find_package(Git QUIET)
if(NOT Git_FOUND)
    message(FATAL_ERROR
        "git is required to apply mew's Google Benchmark patches "
        "(see MEW_BENCHMARK_PATCHES).")
endif()

# UPDATE_DISCONNECTED only checks out a pin the clone already has, and the update
# step would carry patched files across the checkout. On a pin change, reset the
# clone and fetch the new commit first; the patch step then re-applies.
set(_mew_benchmark_src "${FETCHCONTENT_BASE_DIR}/googlebenchmark-src")
if(NOT FETCHCONTENT_SOURCE_DIR_GOOGLEBENCHMARK
   AND IS_DIRECTORY "${_mew_benchmark_src}/.git")
    execute_process(
        COMMAND "${GIT_EXECUTABLE}" -C "${_mew_benchmark_src}" rev-parse HEAD
        OUTPUT_VARIABLE _mew_benchmark_head
        OUTPUT_STRIP_TRAILING_WHITESPACE
        ERROR_QUIET)
    if(NOT _mew_benchmark_head STREQUAL MEW_BENCHMARK_COMMIT)
        message(STATUS "Google Benchmark pin changed; resetting ${_mew_benchmark_src}")
        execute_process(
            COMMAND "${GIT_EXECUTABLE}" -C "${_mew_benchmark_src}" reset --hard --quiet)
        execute_process(
            COMMAND "${GIT_EXECUTABLE}" -C "${_mew_benchmark_src}" clean -fd --quiet)
        execute_process(
            COMMAND "${GIT_EXECUTABLE}" -C "${_mew_benchmark_src}"
                cat-file -e "${MEW_BENCHMARK_COMMIT}^{commit}"
            RESULT_VARIABLE _mew_benchmark_have_commit
            ERROR_QUIET)
        if(NOT _mew_benchmark_have_commit EQUAL 0 AND NOT FETCHCONTENT_FULLY_DISCONNECTED)
            execute_process(
                COMMAND "${GIT_EXECUTABLE}" -C "${_mew_benchmark_src}"
                    fetch --quiet origin "${MEW_BENCHMARK_COMMIT}"
                RESULT_VARIABLE _mew_benchmark_fetch_status)
            if(NOT _mew_benchmark_fetch_status EQUAL 0)
                message(WARNING
                    "Could not fetch Google Benchmark ${MEW_BENCHMARK_COMMIT} into "
                    "${_mew_benchmark_src}; delete that directory to re-clone.")
            endif()
        endif()
    endif()
endif()

FetchContent_Declare(
    googlebenchmark
    GIT_REPOSITORY https://github.com/google/benchmark.git
    GIT_TAG ${MEW_BENCHMARK_COMMIT}
    # Force LF: a global `core.autocrlf=true` (Windows) would check out CRLF while
    # `.gitattributes` keeps the patches LF, and every hunk would fail to apply.
    GIT_CONFIG core.autocrlf=false core.eol=lf
    # PATCH_COMMAND runs without a shell. The patch list is |-joined because a
    # ;-list would be split into separate arguments here.
    PATCH_COMMAND ${CMAKE_COMMAND}
        "-DGIT=${GIT_EXECUTABLE}"
        "-DSRC=<SOURCE_DIR>"
        "-DPATCHES=${_mew_patches_arg}"
        "-DPATCHES_SHA256=${_mew_patches_hash}"
        -P "${CMAKE_CURRENT_SOURCE_DIR}/cmake/apply_patches.cmake"
    # Without this, a reconfigure re-runs the git update step, which resets the
    # tree and drops the patch before PATCH_COMMAND gets a chance to re-run.
    UPDATE_DISCONNECTED TRUE
)
FetchContent_MakeAvailable(googlebenchmark)

# Report the checked-out upstream SHA (excluding patches); fall back to the pin
# for vendored sdists.
set(MEW_BENCHMARK_BUILT_COMMIT "${MEW_BENCHMARK_COMMIT}")
if(Git_FOUND AND IS_DIRECTORY "${googlebenchmark_SOURCE_DIR}/.git")
    execute_process(
        COMMAND "${GIT_EXECUTABLE}" -C "${googlebenchmark_SOURCE_DIR}" rev-parse HEAD
        OUTPUT_VARIABLE MEW_BENCHMARK_BUILT_COMMIT
        OUTPUT_STRIP_TRAILING_WHITESPACE
        RESULT_VARIABLE _mew_rev_parse_status
        ERROR_QUIET)
    if(NOT _mew_rev_parse_status EQUAL 0)
        set(MEW_BENCHMARK_BUILT_COMMIT "${MEW_BENCHMARK_COMMIT}")
    endif()
endif()

# GB versions itself with `git describe --dirty`, which the patches always
# trigger; report them as semver build metadata instead.
get_directory_property(MEW_BENCHMARK_VERSION
    DIRECTORY "${googlebenchmark_SOURCE_DIR}" DEFINITION VERSION)
string(REGEX REPLACE "-dirty$" "" MEW_BENCHMARK_VERSION "${MEW_BENCHMARK_VERSION}")
string(APPEND MEW_BENCHMARK_VERSION "+mew")

message(STATUS "Google Benchmark requested: ${MEW_BENCHMARK_COMMIT}")
message(STATUS "Google Benchmark built:     ${MEW_BENCHMARK_BUILT_COMMIT}")

# --- _core extension ----------------------------------------------------------
set(_MEW_SOURCES
    src/_mew/module.cpp
    src/_mew/state.cpp
    src/_mew/registry.cpp
    src/_mew/reporter.cpp
    src/_mew/managers.cpp
    src/_mew/abort.cpp
)

# nanobind keeps whichever of STABLE_ABI / FREE_THREADED fits the interpreter;
# the limited API has no free-threaded variant.
nanobind_add_module(
    _core
    STABLE_ABI
    FREE_THREADED
    NB_STATIC
    ${_MEW_SOURCES}
)

# Scoped to _core so GB keeps its own warning settings. No -Werror: a newer
# compiler or nanobind must not break the build.
if(MSVC)
    target_compile_options(_core PRIVATE /W4)
else()
    target_compile_options(_core PRIVATE -Wall -Wextra)
endif()

target_include_directories(_core PRIVATE src/_mew)
target_link_libraries(_core PRIVATE benchmark::benchmark)
target_compile_definitions(_core PRIVATE
    MEW_BENCHMARK_COMMIT="${MEW_BENCHMARK_BUILT_COMMIT}"
    MEW_BENCHMARK_VERSION="${MEW_BENCHMARK_VERSION}"
)

# nanobind's stubgen only detects sanitizers from per-target options; without
# them it skips preloading the runtime and aborts on `import _core`.
if(MEW_ASAN)
    target_compile_options(_core PRIVATE -fsanitize=address -fno-omit-frame-pointer)
    target_link_options(_core PRIVATE -fsanitize=address)
endif()
if(MEW_TSAN)
    target_compile_options(_core PRIVATE -fsanitize=thread -fno-omit-frame-pointer)
    target_link_options(_core PRIVATE -fsanitize=thread)
endif()

install(TARGETS _core LIBRARY DESTINATION mew)

# Generated into the build tree so builds never write to the source checkout.
set(_MEW_CORE_STUB "${CMAKE_CURRENT_BINARY_DIR}/_core.pyi")
nanobind_add_stub(
    mew_core_stub
    MODULE _core
    OUTPUT ${_MEW_CORE_STUB}
    PATTERN_FILE ${CMAKE_CURRENT_SOURCE_DIR}/src/_mew/stubgen.pat
    PYTHON_PATH $<TARGET_FILE_DIR:_core>
    DEPENDS _core
)

# The checked-in, formatted stub ships; consumer builds need neither ruff nor
# write access to the source checkout.
install(FILES ${CMAKE_CURRENT_SOURCE_DIR}/src/mew/_core.pyi DESTINATION mew)

execute_process(
    COMMAND ${Python_EXECUTABLE} -m ruff --version
    RESULT_VARIABLE _ruff_available
    OUTPUT_QUIET ERROR_QUIET
)
if(_ruff_available EQUAL 0)
    add_custom_command(
        TARGET mew_core_stub POST_BUILD
        COMMAND ${Python_EXECUTABLE} -m ruff check --fix --exit-zero --quiet
            ${_MEW_CORE_STUB}
        COMMAND ${Python_EXECUTABLE} -m ruff format --quiet
            ${_MEW_CORE_STUB}
        COMMENT "ruff fix + format _core.pyi"
        VERBATIM
    )
endif()

# Explicit maintainer action; normal builds only read the source tree.
add_custom_target(
    update_mew_core_stub
    COMMAND ${CMAKE_COMMAND} -E copy_if_different
        ${_MEW_CORE_STUB}
        ${CMAKE_CURRENT_SOURCE_DIR}/src/mew/_core.pyi
    DEPENDS mew_core_stub
    COMMENT "Updating checked-in src/mew/_core.pyi"
    VERBATIM)

# CI check for a stale checked-in stub. Needs the dev dependencies so ruff gives
# the generated stub its canonical formatting.
add_custom_target(
    check_mew_core_stub
    COMMAND ${CMAKE_COMMAND} -E compare_files
        ${_MEW_CORE_STUB}
        ${CMAKE_CURRENT_SOURCE_DIR}/src/mew/_core.pyi
    DEPENDS mew_core_stub
    COMMENT "Checking checked-in src/mew/_core.pyi"
    VERBATIM)

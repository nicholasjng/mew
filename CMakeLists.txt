cmake_minimum_required(VERSION 3.21)
project(mew LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)

# Emit compile_commands.json for clangd / IDE LSPs. Also force-set here in
# case the project is built without scikit-build-core (e.g. raw `cmake -B`).
set(CMAKE_EXPORT_COMPILE_COMMANDS ON CACHE INTERNAL "")

# The file lands in ${CMAKE_BINARY_DIR}; with build-dir = "build/{wheel_tag}"
# in pyproject.toml that path varies per ABI, so clangd's auto-discovery
# (which probes ./build/ and the source root) won't find it. Symlink it to
# the source root so IDE tooling picks it up without configuration.
add_custom_target(
    symlink_compile_commands ALL
    COMMAND ${CMAKE_COMMAND} -E create_symlink
        ${CMAKE_BINARY_DIR}/compile_commands.json
        ${CMAKE_CURRENT_SOURCE_DIR}/compile_commands.json
    BYPRODUCTS ${CMAKE_CURRENT_SOURCE_DIR}/compile_commands.json
    COMMENT "Symlinking compile_commands.json to source root"
    VERBATIM)

if(NOT CMAKE_BUILD_TYPE)
    set(CMAKE_BUILD_TYPE Release)
endif()

# AddressSanitizer build, driven by -DMEW_ASAN=ON or MEW_ASAN=1 in the env.
# `add_compile_options` covers the fetched googlebenchmark target so its
# objects stay instrumented; the per-target repeat below is required for
# nanobind's stubgen sanitizer detector.
option(MEW_ASAN "Build with AddressSanitizer" OFF)
# Compare the value, not just DEFINED: `MEW_ASAN=0 cmake …` must not enable it.
if(NOT MEW_ASAN AND DEFINED ENV{MEW_ASAN} AND "$ENV{MEW_ASAN}")
    set(MEW_ASAN ON)
endif()
if(MEW_ASAN)
    add_compile_options(-fsanitize=address -fno-omit-frame-pointer)
    add_link_options(-fsanitize=address)
endif()

# ThreadSanitizer build, driven by -DMEW_TSAN=ON or MEW_TSAN=1 in the env.
# Mutually exclusive with ASAN — the two runtimes can't co-load.
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
find_package(Python 3.11 COMPONENTS Interpreter Development.Module REQUIRED)

execute_process(
    COMMAND "${Python_EXECUTABLE}" -m nanobind --cmake_dir
    OUTPUT_STRIP_TRAILING_WHITESPACE
    OUTPUT_VARIABLE nanobind_ROOT
)
find_package(nanobind CONFIG REQUIRED)

# --- Google Benchmark ---------------------------------------------------------
include(FetchContent)

set(MEW_BENCHMARK_COMMIT
    "a8460680f0df91fd26205e0931708a26c3b4094d"
    CACHE STRING "Google Benchmark commit SHA to fetch")

set(BENCHMARK_ENABLE_TESTING OFF CACHE BOOL "" FORCE)
set(BENCHMARK_ENABLE_GTEST_TESTS OFF CACHE BOOL "" FORCE)
set(BENCHMARK_ENABLE_INSTALL OFF CACHE BOOL "" FORCE)
set(BENCHMARK_ENABLE_WERROR OFF CACHE BOOL "" FORCE)

FetchContent_Declare(
    googlebenchmark
    GIT_REPOSITORY https://github.com/google/benchmark.git
    GIT_TAG ${MEW_BENCHMARK_COMMIT}
)
FetchContent_MakeAvailable(googlebenchmark)

# Report the commit FetchContent actually checked out, not just the requested
# tag. A stale or diverged `_deps` checkout (e.g. a CMake cache that kept an old
# pin) would otherwise make BENCHMARK_COMMIT lie about what was really built.
# Falls back to the requested pin when there is no `.git` (vendored sdist).
find_package(Git QUIET)
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

message(STATUS "Google Benchmark requested: ${MEW_BENCHMARK_COMMIT}")
message(STATUS "Google Benchmark built:     ${MEW_BENCHMARK_BUILT_COMMIT}")

# --- _core extension ----------------------------------------------------------
set(_MEW_SOURCES
    src/_mew/module.cpp
    src/_mew/state.cpp
    src/_mew/registry.cpp
    src/_mew/reporter.cpp
)

# nanobind keeps whichever of STABLE_ABI / FREE_THREADED fits the interpreter:
# `cp312` abi3 on stock CPython, a `Py_MOD_GIL_NOT_USED` wheel on free-threaded
# builds (the limited API has no free-threaded variant). Inert on a GIL build.
nanobind_add_module(
    _core
    STABLE_ABI
    FREE_THREADED
    NB_STATIC
    ${_MEW_SOURCES}
)

target_include_directories(_core PRIVATE src/_mew)
target_link_libraries(_core PRIVATE benchmark::benchmark)
target_compile_definitions(_core PRIVATE
    MEW_BENCHMARK_COMMIT="${MEW_BENCHMARK_BUILT_COMMIT}"
)

# Repeat the sanitizer flag on _core itself: nanobind's stubgen detector only
# scans per-target COMPILE_OPTIONS / LINK_OPTIONS. Without this it skips
# preloading libclang_rt.asan and stubgen aborts on `import _core`.
if(MEW_ASAN)
    target_compile_options(_core PRIVATE -fsanitize=address -fno-omit-frame-pointer)
    target_link_options(_core PRIVATE -fsanitize=address)
endif()
if(MEW_TSAN)
    target_compile_options(_core PRIVATE -fsanitize=thread -fno-omit-frame-pointer)
    target_link_options(_core PRIVATE -fsanitize=thread)
endif()

install(TARGETS _core LIBRARY DESTINATION mew)

# Emit the .pyi stub next to the Python sources so IDEs and type checkers
# pick it up via the src/-layout (which shadows site-packages for `mew._core`).
nanobind_add_stub(
    _core_stub
    MODULE _core
    OUTPUT ${CMAKE_CURRENT_SOURCE_DIR}/src/mew/_core.pyi
    PATTERN_FILE ${CMAKE_CURRENT_SOURCE_DIR}/src/_mew/stubgen.pat
    PYTHON_PATH $<TARGET_FILE_DIR:_core>
    DEPENDS _core
)

# Run `ruff format` on the freshly-generated stub. Skipped if ruff isn't
# available (e.g. building from an sdist without dev extras).
execute_process(
    COMMAND ${Python_EXECUTABLE} -m ruff --version
    RESULT_VARIABLE _ruff_available
    OUTPUT_QUIET ERROR_QUIET
)
if(_ruff_available EQUAL 0)
    add_custom_command(
        TARGET _core_stub POST_BUILD
        COMMAND ${Python_EXECUTABLE} -m ruff check --fix --exit-zero --quiet
            ${CMAKE_CURRENT_SOURCE_DIR}/src/mew/_core.pyi
        COMMAND ${Python_EXECUTABLE} -m ruff format --quiet
            ${CMAKE_CURRENT_SOURCE_DIR}/src/mew/_core.pyi
        COMMENT "ruff fix + format _core.pyi"
        VERBATIM
    )
endif()

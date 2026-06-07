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

# AddressSanitizer build. Driven by -DMEW_ASAN=ON (or MEW_ASAN=1 in the env
# from scikit-build-core). The flag goes onto the _core target's COMPILE_OPTIONS
# / LINK_OPTIONS (not CMAKE_CXX_FLAGS) so nanobind's stubgen sanitizer-detector
# (nanobind PR #1000) finds it and preloads libclang_rt.asan via DYLD_INSERT_LIBRARIES
# when importing _core during stub generation — putting the flag on the global
# CMAKE_CXX_FLAGS works for the compiler but not for the detector, which walks
# per-target properties only. add_compile_options also covers the fetched
# googlebenchmark target so its objects stay ASAN-instrumented.
option(MEW_ASAN "Build with AddressSanitizer" OFF)
if(DEFINED ENV{MEW_ASAN} AND NOT MEW_ASAN)
    set(MEW_ASAN ON)
endif()
if(MEW_ASAN)
    add_compile_options(-fsanitize=address -fno-omit-frame-pointer)
    add_link_options(-fsanitize=address)
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

set(BENCHMARK_ENABLE_TESTING OFF CACHE BOOL "" FORCE)
set(BENCHMARK_ENABLE_GTEST_TESTS OFF CACHE BOOL "" FORCE)
set(BENCHMARK_ENABLE_INSTALL OFF CACHE BOOL "" FORCE)
set(BENCHMARK_ENABLE_WERROR OFF CACHE BOOL "" FORCE)

FetchContent_Declare(
    googlebenchmark
    GIT_REPOSITORY https://github.com/google/benchmark.git
    GIT_TAG main
    GIT_SHALLOW TRUE
)
FetchContent_MakeAvailable(googlebenchmark)

# Capture the commit hash of the fetched googlebenchmark checkout so it can
# be reported from Python — useful when diagnosing bugs that turn out to
# depend on a particular GB revision.
execute_process(
    COMMAND git rev-parse HEAD
    WORKING_DIRECTORY ${googlebenchmark_SOURCE_DIR}
    OUTPUT_VARIABLE _benchmark_commit
    OUTPUT_STRIP_TRAILING_WHITESPACE
    ERROR_QUIET
    RESULT_VARIABLE _benchmark_commit_rv
)
if(NOT _benchmark_commit_rv EQUAL 0 OR NOT _benchmark_commit)
    set(_benchmark_commit "unknown")
endif()
message(STATUS "Google Benchmark commit: ${_benchmark_commit}")

# --- _core extension ----------------------------------------------------------
set(_MEW_SOURCES
    src/_mew/module.cpp
    src/_mew/state.cpp
    src/_mew/registry.cpp
    src/_mew/reporter.cpp
)

nanobind_add_module(
    _core
    STABLE_ABI
    NB_STATIC
    ${_MEW_SOURCES}
)

target_include_directories(_core PRIVATE src/_mew)
target_link_libraries(_core PRIVATE benchmark::benchmark)
target_compile_definitions(_core PRIVATE
    MEW_BENCHMARK_COMMIT="${_benchmark_commit}"
)

# Repeat -fsanitize=address on the _core target itself: add_compile_options sets
# the *directory*'s COMPILE_OPTIONS, but nanobind's stubgen sanitizer detector
# (PR #1000, see nanobind_sanitizer_preload_env in nanobind-config.cmake) only
# scans per-target COMPILE_OPTIONS / LINK_OPTIONS. Without this, the detector
# concludes "no sanitizer" and runs stubgen without preloading libclang_rt.asan,
# which then aborts at `import _core` with "Interceptors are not working".
if(MEW_ASAN)
    target_compile_options(_core PRIVATE -fsanitize=address -fno-omit-frame-pointer)
    target_link_options(_core PRIVATE -fsanitize=address)
endif()

install(TARGETS _core LIBRARY DESTINATION mew)

# Generate a .pyi stub for the compiled module alongside the Python package
# source so IDEs and type checkers (Zed/ty/Pyright) pick it up without
# inspecting the installed wheel — the src/-layout otherwise wins over
# site-packages when resolving `mew._core`. The stub is regenerated on every
# build and committed to version control; scikit-build-core packs it into
# the wheel automatically via `wheel.packages`.
nanobind_add_stub(
    _core_stub
    MODULE _core
    OUTPUT ${CMAKE_CURRENT_SOURCE_DIR}/src/mew/_core.pyi
    PYTHON_PATH $<TARGET_FILE_DIR:_core>
    DEPENDS _core
)

# Run `ruff format` on the freshly-generated stub so it matches the rest of
# the codebase's style. Skipped silently if ruff isn't available (e.g. when
# building from an sdist without dev extras).
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

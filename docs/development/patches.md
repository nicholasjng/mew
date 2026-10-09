# Google Benchmark patches

`mew` builds against a pinned Google Benchmark commit (`_mew_benchmark_default_commit`
in `CMakeLists.txt`) and applies the patches in `patches/` on top of it, in order.
FetchContent runs `cmake/apply_patches.cmake` as its patch step. A stamp file in the
fetched tree records the base commit and each patch's hash; when either changes, the
script resets the tree to the pin and re-applies the whole series.

Because of the patches, the build reports Google Benchmark's version with `+mew`
build metadata, for example `v1.9.5-144-ge662de9a+mew`. The mew version identifies
the exact patch series, since the series only changes between mew releases.
A build against your own checkout (`-DFETCHCONTENT_SOURCE_DIR_GOOGLEBENCHMARK=...`)
skips the patch step, so apply the series to it yourself; mew does not compile
against an unpatched tree.

## Moving the pin

1. Set `_mew_benchmark_default_commit` to the new commit. Existing build trees
   follow the new default and fetch the commit on the next configure.
2. Rebuild. If a patch no longer applies, the configure fails and names it. Rebase
   it in a scratch clone: check out the new pin, apply the patches before it with
   `git apply`, commit, redo the failing patch's change by hand, and write
   `git diff` back to its file. Repeat for each later patch that fails.
3. Run the native regressions in a Debug build, where Google Benchmark's checks
   and asserts are active:

   ```console
   $ uv run --no-sync python scripts/test-benchmark-patches.py build/<wheel_tag>/_deps/googlebenchmark-src
   ```

4. Run the Python suite in both the default and the free-threaded environment (see
   {doc}`building`), refresh the stub with `update_mew_core_stub` (it embeds the
   version string), and update the pin in `docs/changelog.md`.

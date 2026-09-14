# Crafting a release

`.github/workflows/release.yml` builds and publishes on a *published* GitHub release,
through trusted publishing in the `pypi` environment.
Tags are `vX.Y.Z`; the version lives in `src/mew/__init__.py` (scikit-build-core reads it
from there) and `uv.lock` does not pin it.

Wheels come from cibuildwheel on four runners — `cp311`, a `cp312` stable-ABI wheel
covering 3.12+, and `cp314t` — plus an sdist. Everything else installs from source.

## 1. Bump the version

```python
# src/mew/__init__.py
__version__ = "X.Y.Z"
```

## 2. Close the changelog section

Entries accumulate under `## Version X.Y.Z (unreleased)` in `docs/changelog.md` during
the cycle; write them for users, not for the commit log. Replace `unreleased` with the
release date (`Mon D, YYYY`) to close the section.

## 3. Check

```bash
uv run pytest -q
uvx prek run --all-files
uv run --no-sync python scripts/configure-clangd.py
cmake --build build/clangd --target check_mew_core_stub
uv run --group docs sphinx-build -W -b html docs docs/_build/html
uv run mew --version    # mew X.Y.Z (Google Benchmark ...)
```

## 4. Land it on master

```bash
jj commit -m "mew vX.Y.Z"
jj bookmark set master -r @-
jj git push --bookmark master
```

## 5. Publish

Creates the tag and triggers the workflow:

```bash
gh release create vX.Y.Z --target master --title "mew-bench X.Y.Z" --notes "..."
```

The notes are the `## Version X.Y.Z` section of `docs/changelog.md` — paste it into
`--notes`, or have changelog tooling emit it. Leaving the notes flags off makes `gh`
prompt instead, which you can skip and fill in on the release page afterwards: the
workflow reacts to the release being *published*, not to its body.

## 6. Verify

```bash
gh run list --workflow=release.yml --limit 1
uv run --with mew-bench==X.Y.Z --no-project -- mew --version
```

## 7. Open the next cycle

Add a fresh `## Version X.Y.Z (unreleased)` heading above the release you just cut.

## Notes

- Bump the minor for behaviour changes, the patch for fixes and docs; pre-1.0, breaking
  changes go in a minor.
- A `release` event runs the workflow file from **master**, not from the tag. Changes to
  `release.yml` must be landed before the release that should use them.
- `jj git push` cannot push tags, which is why step 5 lets GitHub create it.
- The publish job rejects any artifact whose filename doesn't carry the tag's version, so
  a tag that disagrees with `__version__` fails before upload rather than after.
- A failed publish cannot be retried against the same version: PyPI filenames are
  immutable, so bump and release again.

# Contributing

All contributions are welcome.

## Suggested workflow

1. Fork and clone the `nicholasjng/mew` repository on GitHub.
2. Run `uv sync --all-extras --all-groups` to set up the editable install with all dev deps.
3. Make your changes, add tests under `tests/` if necessary.
4. Run `uv run pytest tests/ -q`, `uvx prek run --all-files`, and `uvx ty check src/mew tests/` before pushing.
5. Open a pull request.

## Style

- **Code**: `ruff` for lint + format, `ty` for type-checking.
- **Docstrings**: NumPy style.
- **Comments**: Add one only when the _why_ is non-obvious.

## Tests

`tests/` is the source of truth for behavior.
When adding a new public API, please add a test that covers the happy path and one for any documented error cases.

## Docs

If your change touches public API or CLI surface, update the corresponding page under `docs/guide/` or `docs/reference/`.
Build the docs locally with:

```console
$ uv run sphinx-build -W --keep-going -b html docs docs/_build/html
```

The docs CI job mirrors readthedocs' `fail_on_warning: true`; broken cross references and unresolved intersphinx links will fail the build.

## Reporting bugs

Please open a GitHub issue, ideally with:

- `mew --version` output (includes the bundled Google Benchmark commit).
- Minimal reproduction — a `bench_*.py` and the exact `mew run` invocation.
- What you expected vs. what actually happened.

# Contributing

All contributions are welcome.

## Suggested workflow

1. Fork and clone the `nicholasjng/mew` repository on GitHub.
2. Run `uv sync --all-extras --all-groups` to set up the editable install with all dev deps.
3. Make your changes, add tests under `tests/` if necessary.
4. Run `uv run pytest tests/ -q` and `uvx prek run --all-files` before pushing.
5. Open a pull request.

## Style

- **Code**: `ruff` for lint + format, `ty` for type-checking.
- **Docstrings**: NumPy style.
- **Comments**: Add one only when the _why_ is non-obvious.

## Tests

When adding a public API, please add a test covering the happy path and one for any documented error cases.
The `test` dependency group contains the core test dependencies.
On GIL builds, add `--group duckdb` to enable the SQL archive integration tests.
Free-threaded and TSAN environments should omit that group, since duckDB does not ship free-threaded wheels.

## Docs

If your change touches public API or CLI surface, update the corresponding page under `docs/guide/`, `docs/howto/`, or `docs/reference/`.
Keep one sentence per source line in documentation prose, including list items.
Do not use semicolons in flowing text.
Let the renderer wrap paragraphs.
Do not hard-wrap sentences at a column limit.
Keep code blocks, tables, and directive syntax in the format they need.
Write examples around a concrete task and explain what the reader measures or learns.
Put task-focused pages under `docs/howto/` and production workflows spanning several features under `docs/recipes/`.
For a project success story, include the revision pair, environment, raw measurements, and confirmed outcome.
Build the docs locally with:

```console
$ uv run --group docs sphinx-build -W --keep-going -b html docs docs/_build/html
```

The docs CI job mirrors readthedocs' `fail_on_warning: true`.
Broken cross references and unresolved intersphinx links will fail the build.

## Reporting bugs

Please open a GitHub issue, ideally with:

- `mew --version` output (includes the bundled Google Benchmark commit).
- Minimal reproduction: a `bench_*.py` and the exact `mew run` invocation.
- What you expected vs. what actually happened.

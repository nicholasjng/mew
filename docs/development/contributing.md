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
When adding a public API, please add a test covering the happy path and one for any documented error cases.

### Native profiler backends (py-spy / perf)

`tests/test_profilers_native.py` exercises the out-of-process backends behind
`mew profile`. They skip unless the tool is actually *usable* on the host, so
they stay inert in normal runs and on macOS — opt in by running them where the
tooling exists. They aren't in CI: the privilege requirements below don't fit
GitHub-hosted runners, so these backends are validated locally / on a real Linux
box.

**py-spy** runs cleanly in a container; the repo ships an image for it:

```console
$ docker build -f docker/profile.Dockerfile -t mew-profile .
$ docker run --rm --cap-add=SYS_PTRACE mew-profile   # --native needs CAP_SYS_PTRACE
```

**perf** needs recording access that `perf_event_paranoid` gates. On your own
Linux box, lower it once, then run the perf test directly:

```console
$ sudo sysctl kernel.perf_event_paranoid=1
$ uv run pytest tests/test_profilers_native.py -k perf
```

Locked-down hosts can't do that — GitHub-hosted runners ship
`perf_event_paranoid=4` and won't let you change it. There, grant the capability
instead via a privileged/capped container (`docker run --cap-add=PERFMON
--cap-add=SYS_ADMIN`, or `--privileged`). Where neither is possible the perf test
skips rather than fails: the backend's `unavailable_reason()` probes whether perf
can record, and the test reuses that verdict.

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

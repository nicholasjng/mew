# Track performance through Git history

You know a workload was faster last week, but several changes have landed since then.
This recipe records one JAX workload at each revision, compares a known good and bad revision, and uses Git bisection to narrow the slowdown to a commit.
It also gives you a measurement command to reuse in a scheduled benchmarking job.

mew runs the benchmark and compares the results.
Git chooses the revisions.
The scripts below install and measure them.

## Choose what to track

The example measures the elapsed time of a warm, JIT-compiled matrix multiplication on CPU.
Input creation and the first compiled call happen before the timing loop.
Each timed call waits for the result, so the measurement includes dispatch, execution, and synchronization.
These choices follow [JAX's benchmarking guidance](https://docs.jax.dev/en/latest/201/profiling.html#benchmarking-jax-code).

Use this as a template for the workload that regressed in your application.
A matrix multiplication may be dominated by native code and show little change across Python-only JAX revisions.
If your question concerns tracing or compilation time, write a separate benchmark with that work inside the timed region and control compilation caches explicitly.

Keep the workload, input shape, dtype, device, interpreter, and dependencies fixed across the revision range.
This example varies the Python JAX checkout while keeping a prebuilt `jaxlib` fixed.
Choose a range where that `jaxlib` is compatible with every revision, following [JAX's version compatibility rules](https://docs.jax.dev/en/latest/jep/9419-jax-versioning.html).
To investigate changes in XLA or native JAX code, build the appropriate `jaxlib` for each revision using [JAX's source-build instructions](https://docs.jax.dev/en/latest/developer.html).
That requires a different build step from the editable Python installation used here.

## Keep the harness outside the checkout

Use a separate worktree so checking out old commits does not change your benchmark or scripts.
The commands below assume a POSIX shell and an existing JAX clone at `/path/to/jax`.
Replace `GOOD_REF` and `BAD_REF` with the revisions you want to compare.

```console
$ cd /path/to/jax
$ git worktree add --detach ../jax-history GOOD_REF
$ mkdir ../jax-bench
$ cd ../jax-bench
$ python3.12 -m venv .venv
$ .venv/bin/python -m pip install --upgrade pip setuptools wheel
$ .venv/bin/python -m pip install mew-bench -e '../jax-history[cpu]'
$ .venv/bin/python -m pip freeze --all > environment.txt
```

The initial install resolves dependencies for the good revision using [JAX's editable installation](https://docs.jax.dev/en/latest/contributing.html).
Check the bad revision in the next steps before starting a longer run.
If the endpoints need different dependency versions, narrow the range or prepare compatible builds first.
Keep `environment.txt` with the results and reuse the same environment for the entire investigation.
The measurement script uses `--no-deps --no-build-isolation` to keep installed runtime and build dependencies unchanged when switching JAX revisions.

Your directories should look like this:

```text
jax/                  # your existing clone
jax-history/          # disposable worktree under test
jax-bench/
    .venv/
    environment.txt
    pyproject.toml
    conf.py
    bench_jax.py
    measure.sh
    bisect.sh
```

Save the files below in `jax-bench/`.
They are also available to download: {download}`conf.py <_code/conf.py>`, {download}`bench_jax.py <_code/bench_jax.py>`, {download}`measure.sh <_code/measure.sh>`, and {download}`bisect.sh <_code/bisect.sh>`.
Run all commands that switch revisions against `jax-history`, leaving the original clone available for your work.

## Write the benchmark

```{literalinclude} _code/bench_jax.py
:language: python
```

The import check catches accidentally benchmarking an installed release instead of the worktree.
The fixed name `jax_matmul` lets rows match across revisions.

Record what each run measured with a setup file.
`pyproject.toml` names it:

```toml
[tool.mew]
setup = "conf.py"
```

```{literalinclude} _code/conf.py
:language: python
```

`mew run` calls `context()` once per run and stores the result in every row: the source revision, JAX and `jaxlib` versions, selected devices, and a hash of the benchmark file.
mew adds machine and interpreter information automatically.

## Measure a revision

```{literalinclude} _code/measure.sh
:language: bash
```

Each invocation starts a fresh process and records ten repetitions.
It writes a JSON snapshot for the current commit to `results/` and appends a run to `history.jsonl`.
The snapshot comes from redirected JSON on stdout.
`--append` applies to the JSONL archive.
The final check rejects skipped, missing, or invalid measurements before they can be used for classification.
The run name defaults to the full commit hash.
An optional argument names the run instead, such as `good` or `bad`.

The snapshots are what `mew compare` reads.
Re-running a commit overwrites its snapshot and adds another run to the archive.
The archive keeps every run for [later analysis](analyzing-results.md).

## Check the endpoints

From `jax-bench/`, measure the good revision and preserve its baseline:

```console
$ sh measure.sh good
$ cp results/$(git -C ../jax-history rev-parse HEAD).json baseline.json
$ git -C ../jax-history switch --detach BAD_REF
$ sh measure.sh bad
$ .venv/bin/mew compare baseline.json results/$(git -C ../jax-history rev-parse HEAD).json
```

Repeat the good measurement before proceeding to estimate its ordinary variation.
Confirm that the bad revision is consistently slower than the baseline by more than your chosen threshold.
The scripts use 5% as an example.
Change it to match the noise and impact of your workload.
Keep the baseline fixed during bisection so every commit answers the same question.

## Build a history archive

You can measure a sequence before deciding where to bisect:

```bash
git -C ../jax-history rev-list --first-parent --reverse GOOD_REF..BAD_REF > commits.txt
while read -r commit; do
    git -C ../jax-history switch --detach "$commit" || break
    sh measure.sh || break
done < commits.txt
```

This walks the first-parent history after the good revision through the bad revision, including merge commits on that path.
The loop stops at the first failed measurement so you can inspect it.
List the runs and compare two recorded commits with:

```console
$ .venv/bin/mew log history.jsonl
$ .venv/bin/mew compare results/GOOD_COMMIT_SHA.json results/BAD_COMMIT_SHA.json
```

Replace those placeholders with the full hashes, which `mew log` shows as run names.
Use the [archive analysis recipe](analyzing-results.md) to summarize timings by revision.

For continuous benchmarking, call `measure.sh` after checking out each new revision on a dedicated runner.
Serialize jobs so measurements do not compete for the machine or write to the same archive concurrently.
Restore the fixed environment, record the exact source revision, and retain the JSON results, archive, environment file, harness, and build logs as job artifacts.
Keep a baseline from the same runner and compare the new commit's JSON snapshot against it with the gate command in `bisect.sh`.
Decide explicitly when to replace the baseline.
Changing it after every run can hide a series of small slowdowns.
When the interpreter, device, dependencies, or workload changes, start a new series and establish a new baseline.

## Bisect the slowdown

[Git's `bisect run` contract](https://git-scm.com/docs/git-bisect#_bisect_run) uses `0` for good, `1` for bad, and `125` to skip a commit that cannot be tested.
mew uses `3` for a regression and `1` for no matching benchmarks, so passing the comparison command directly to Git would misclassify an empty comparison as bad.
This wrapper translates the outcomes:

```{literalinclude} _code/bisect.sh
:language: bash
```

Run it against the disposable worktree from `jax-bench/`:

```console
$ git -C ../jax-history bisect start BAD_REF GOOD_REF
$ git -C ../jax-history bisect run sh "$PWD/bisect.sh"
$ git -C ../jax-history bisect log > bisect.log
$ git -C ../jax-history bisect reset
```

An installation failure or unusable measurement skips that revision.
Inspect the logs when this happens.
Many skipped revisions can leave several possible culprit commits.
Stop the investigation and fix the environment if failures come from the harness or dependencies rather than a particular revision.
The regression gate compares median times against the threshold.
It does not require a statistical significance marker.

## Confirm the result

Bisection assumes the workload stays slow after the regression is introduced.
A later fix, multiple regressions, or machine drift can violate that assumption.
Measure the reported commit and its relevant parent again, alternating between them several times on the same machine.
Append each side's runs to its own file and compare the two files, keeping their raw repetitions.
See [comparisons across processes](../howto/ab-comparison.md#alternate-the-sides) for the loop.

Once the slowdown is reproducible, inspect the change and profile the affected workload.
Keep a record of the measured delta, spread, revision pair, and eventual fix so the investigation can become a project case study.

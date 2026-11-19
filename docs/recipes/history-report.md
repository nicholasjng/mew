# Publish a history report

An archive of runs answers "when did this get slower?", but only if someone reads it.
This recipe appends a run to an archive every night, writes `mew report`'s page from it, and publishes the page with GitHub Pages.

The page is one HTML file with no external requests.
It shows each machine and metric as its own view:

- a suite chart, the geometric mean of every benchmark over its typical value, to choose a range;
- the steps in that range, newest first, each with the commits it could come from;
- a chart per benchmark, or a table with a sparkline row per benchmark when there are more than 12.

## Record the commit

`mew report` orders runs by the commit they measured, so every run must record one.
Return {func}`mew.vcs_context` from the setup file's `context()`:

```python
# benchmarks/conf.py
import mew


def context() -> dict:
    return mew.vcs_context()
```

```toml
[tool.mew]
setup = "benchmarks/conf.py"
```

Without a commit, the page orders runs by date and names no commits.

## Write the page

Append each run to a JSONL archive, then write the page from the project's checkout:

```console
$ mew run --repetitions 5 --append -o history.jsonl.gz
$ mew report history.jsonl.gz -o report.html
Wrote report.html (639 KiB): 600 runs on 2 machines, 19 steps in real time
```

`mew report` reads the rest from the project it runs in:

- **Order.** Runs follow the first-parent history of `-r/--revision`, `HEAD` by default, rather than their dates, so a run backfilled at an old commit lands where its code belongs.
  A run at a commit the repository has but that history doesn't, such as one on a branch merged later, is drawn hollow and left out of the steps.
- **Thresholds.** `[tool.mew.regressions]` sets the step threshold, as it does the `mew compare` gate, and `[[tool.mew.counters]]` sets which direction is better.
- **Links.** Commits link to the forge named by `[project.urls]` `Source` or `Repository`, else by the `origin` remote, for GitHub, GitLab, Codeberg, Gitea and Forgejo.

`--where` selects rows as in `mew compare`, for example one machine's runs with `--where context.host=bench-1`.
If the benchmarks live outside the repository they measure, run `mew report` from that repository's checkout, as the [Git history recipe](git-history.md#build-a-history-archive) does.

## Read the page

A point is one commit: reruns of a commit pool into one point, whose band shows the spread of every repetition.
A run with uncommitted edits stays its own point, drawn hollow.

A step is a lasting change, not one slow run.
At each point, mew compares the 5 points before it with the 5 from it on, and reports a step when:

- every point on one side lies past every point on the other;
- their medians differ by at least the regression threshold, 5% by default;
- that difference is at least twice the benchmark's noise.

So the newest 5 points always wait for confirmation, and a history needs about 10 points before any step shows.
A benchmark whose code changed, as its recorded `version` says, starts a new line, and no step spans the change.

Each benchmark shows its noise, as in `±1.4%`: the spread of its points from one commit to the next.
It turns amber when twice the noise is past the threshold, because only steps larger than twice the noise count there.

A step in the worse direction that an [allowlist](../guide/comparing.md#allowlist) rule covers is grey and marked "allowed", with the rule's reason on hover.

Click a point to pin it, and shift-click another point of the same chart to compare the two.
Clicking a step in the list does the same for the points around it.
The panel shows the change, lists the commits between the points, and gives a command that runs `mew compare` on exactly those runs.

The URL keeps the machine, metric, range and layout, so a link opens the view you were looking at.

## Publish it every night

The workflow below runs on a dedicated runner, which keeps the archive between runs.
It appends tonight's run, writes the page, and deploys it to GitHub Pages.
Enable Pages with "GitHub Actions" as the source in the repository settings first.

```yaml
name: nightly benchmarks

on:
  schedule:
    - cron: "0 2 * * *"
  workflow_dispatch:

concurrency:
  group: benchmarks
  cancel-in-progress: false

jobs:
  bench:
    runs-on: [self-hosted, bench]
    permissions:
      pages: write
      id-token: write
    environment:
      name: github-pages
      url: ${{ steps.deploy.outputs.page_url }}
    steps:
      - uses: actions/checkout@v4
        with: { fetch-depth: 0 }
      - uses: astral-sh/setup-uv@v6
      - name: Measure
        run: |
          echo "ARCHIVE=$HOME/benchmarks/history.jsonl.gz" >> "$GITHUB_ENV"
          mkdir -p "$HOME/benchmarks"
          uv sync --all-extras
          ARCHIVE="$HOME/benchmarks/history.jsonl.gz"
          uv run mew run --repetitions 5 --append -o "$ARCHIVE"
      - name: Report
        run: |
          mkdir site
          uv run mew report "$ARCHIVE" -o site/index.html
          cp "$ARCHIVE" site/
      - uses: actions/upload-pages-artifact@v3
        with: { path: site }
      - id: deploy
        uses: actions/deploy-pages@v4
```

`fetch-depth: 0` gives `mew report` the history it orders runs along; in a shallow clone, older commits go by their run's date, as bare hashes.
The archive lives in the runner's home directory, outside the checkout, so it survives from one night to the next.
Copying it into the site also backs it up with every deployment.

A job on a shared runner measures noise as much as code: use a dedicated machine, as the [regression gate recipe](ci-gate.md) explains.
To report several machines, let each append to its own archive, and write a page per machine, or concatenate the archives first (`cat a.jsonl.gz b.jsonl.gz > all.jsonl.gz` works for gzip too): `mew report` shows each machine as its own view.

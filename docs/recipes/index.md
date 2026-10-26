# Recipes

Recipes combine benchmarks, commands, and result analysis into workflows for running mew in production.
Start with the one closest to your problem.

| Task | Recipe | What you get |
| --- | --- | --- |
| Stop regressions before they merge | [Gate pull requests on regressions](ci-gate.md) | A same-machine, alternating base-against-head job with a regression gate. |
| Find when a project became slower | [Track performance through Git history](git-history.md) | A JAX benchmark, a revision archive, and an automated bisection. |
| Query a growing archive of runs | [Analyze saved results](analyzing-results.md) | Run extraction, timing summaries, SQL queries, and Parquet export. |

#!/bin/sh
set -eu

harness=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$harness"
python="$harness/.venv/bin/python"
repo="$harness/../jax-history"
commit=$(git -C "$repo" rev-parse HEAD)
name=${1:-$commit}
mkdir -p results

# Keep the installed jaxlib and other dependencies fixed across revisions.
"$python" -m pip install --no-deps --no-build-isolation -e "$repo"
"$harness/.venv/bin/mew" run "$harness/bench_jax.py" \
    --min-time 1s --min-warmup-time 200ms --repetitions 10 \
    --run-name "$name" --append \
    --format json -o - -o history.jsonl > "results/$commit.json"

# A successful CLI invocation can still contain skipped measurements.
"$python" - "results/$commit.json" <<'PY'
import math
import sys

from mew.compare import read_results

rows = read_results(sys.argv[1])
if len(rows) != 10 or any(
    row["skip"] is not None
    or row["name"] != "jax_matmul"
    or not math.isfinite(row["real_time"])
    or row["real_time"] <= 0
    for row in rows
):
    raise SystemExit("Expected ten valid jax_matmul measurements")
PY

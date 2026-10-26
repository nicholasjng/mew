#!/bin/sh
set -u

harness=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo="$harness/../jax-history"

# A missing baseline or runner is a harness error; abort the bisection.
test -s "$harness/baseline.json" || exit 128
test -x "$harness/.venv/bin/mew" || exit 128

sh "$harness/measure.sh"
status=$?
case "$status" in
    0) ;;
    130|143) exit 128 ;;
    *) exit 125 ;;
esac

commit=$(git -C "$repo" rev-parse HEAD) || exit 128
cd "$harness" || exit 128
"$harness/.venv/bin/mew" compare \
    --metric real_time \
    --regression-threshold 5% --exit-non-zero-on-regression \
    baseline.json "results/$commit.json"
status=$?
case "$status" in
    0) exit 0 ;;    # Good: within the threshold.
    3) exit 1 ;;    # Bad: mew found a regression.
    1|2) exit 125 ;;  # Untestable: no comparison or invalid results.
    *) exit 128 ;;  # Abort on unexpected failures.
esac

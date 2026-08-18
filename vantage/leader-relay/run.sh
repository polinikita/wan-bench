#!/usr/bin/env bash
# Question: goodput, latency, and wire overhead under leader-relay faults on a
# 20-node committee across the offered-load ladder.
#
#   ./run.sh             three repetitions (fresh fleet each), skipping existing ones
#   REPS=N ./run.sh      a different repetition count
#   ./run.sh summarize   median across repetitions per (variant, rate), with spread
#
# Fleet: 20 validators + 1 control, c5d.2xlarge, eu-west-1a. Measured load fits
# 4 vCPU (max 2.2 cores, autobahn all-to-all at 200k), but the 21-instance fleet
# keeps 8 vCPU / 16 GB for headroom so the fault behavior stays unconfounded.
set -euo pipefail
cd "$(dirname "$0")/../.."

CONFIG=configs/n20-leader-relay-scaling.yaml
OUT_BASE=results/vantage/leader-relay
REPS="${REPS:-3}"

if [ "${1:-run}" = "summarize" ]; then
    exec python3 vantage/summarize.py "$OUT_BASE"
fi

mkdir -p "$OUT_BASE"
for rep in $(seq 1 "$REPS"); do
    out="$OUT_BASE/rep-$rep"
    if [ -e "$out" ]; then
        echo "run.sh: $out exists, skipping (delete it to rerun)"
        continue
    fi
    python3 -m wanbench.cli campaign --config "$CONFIG" --out "$out" --execute \
        2>&1 | tee "$OUT_BASE/rep-$rep.log"
done

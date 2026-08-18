#!/usr/bin/env bash
# Question: how latency, CPU, and wire cost grow with the committee size
# (n = 10, 20, 50, 100) at a fixed light load of 100 tx/s.
#
#   ./run.sh             three repetitions (fresh fleet each), skipping existing ones
#   REPS=N ./run.sh      a different repetition count
#   ./run.sh summarize   median across repetitions per (variant, n), with spread
#
# Fleet: up to 100 validators + 1 control, c5d.xlarge, eu-west-1a. 4 vCPU / 8 GB
# fits this question (heaviest measured cell: sailfish-pp at 2.87 cores, n=100).
set -euo pipefail
cd "$(dirname "$0")/../.."

CONFIG=configs/paper-committee-scaling.yaml
OUT_BASE=results/vantage/committee-scaling
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

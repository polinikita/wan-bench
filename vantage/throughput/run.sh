#!/usr/bin/env bash
# Question: committed throughput and end-to-end latency of Vantage against the
# baseline protocols on a 100-node emulated WAN, across the offered-load ladder.
#
#   ./run.sh             three repetitions (fresh fleet each), skipping existing ones
#   REPS=N ./run.sh      a different repetition count
#   ./run.sh summarize   median across repetitions per (variant, rate), with spread
#
# Fleet: 100 validators + 1 control, c5d.2xlarge, eu-west-1a. Sizing is measured:
# the median node needs 3.0-3.2 cores at 250-275k (peaks above 4; sailfish-pp needs
# 4.2 at 150k) and up to 7.1 GiB of memory, so 8 vCPU / 16 GB is required.
# Expect several hours per repetition (8 variants x 7 rate points).
set -euo pipefail
cd "$(dirname "$0")/../.."

CONFIG=configs/paper-n100-throughput.yaml
OUT_BASE=results/vantage/throughput
REPS="${REPS:-3}"

if [ "${1:-run}" = "summarize" ]; then
    exec python3 vantage/summarize.py "$OUT_BASE"
fi

mkdir -p "$OUT_BASE"
for rep in $(seq 1 "$REPS"); do
    out="$OUT_BASE/rep-$rep"
    status=$(python3 -c 'import json,sys
try: print(json.load(open(sys.argv[1] + "/campaign.json")).get("status", "absent"))
except OSError: print("absent")' "$out")
    case "$status" in
        completed|completed_with_failures)
            echo "run.sh: $out is $status, skipping (delete it to rerun)"
            continue ;;
        absent) resume=() ;;
        *) echo "run.sh: $out is '$status', resuming"; resume=(--resume) ;;
    esac
    python3 -m wanbench.cli campaign --config "$CONFIG" --out "$out" --execute \
        ${resume[@]+"${resume[@]}"} \
        2>&1 | tee -a "$OUT_BASE/rep-$rep.log"
done

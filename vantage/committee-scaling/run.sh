#!/usr/bin/env bash
# Question: how latency, CPU, and wire cost grow with the committee size
# (n = 10, 20, 50, 100) at a fixed light load of 100 tx/s.
#
#   ./run.sh             three repetitions (fresh fleet each), skipping existing ones
#   REPS=N ./run.sh      a different repetition count
#   ./run.sh summarize   median across repetitions per (variant, n), with spread
#
# Fleet: up to 100 validators + 1 control, c5d.2xlarge, eu-west-1a — the same
# hardware as every other question, so all figures share one platform.
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
    status=$(python3 -c 'import json,sys
for f in ("campaign.json", "matrix.json"):
    try:
        print(json.load(open(sys.argv[1] + "/" + f)).get("status", "absent")); break
    except OSError: pass
else: print("absent")' "$out")
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

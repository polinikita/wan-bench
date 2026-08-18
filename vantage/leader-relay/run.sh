#!/usr/bin/env bash
# Question: goodput, latency, and wire overhead under leader-relay faults on a
# 20-node committee across the offered-load ladder.
#
# Each protocol runs its OWN campaign: the relay attack leaks state across
# variants deployed on a reused fleet (first variant measures, later ones
# starve), so the recorded reference trio's methodology applies — one fleet
# per protocol per repetition.
#
#   ./run.sh             three repetitions, skipping completed ones
#   REPS=N ./run.sh      a different repetition count
#   ./run.sh summarize   median across repetitions per (variant, rate), with spread
set -euo pipefail
cd "$(dirname "$0")/../.."

OUT_BASE=results/vantage/leader-relay
REPS="${REPS:-3}"
TRIO=(autobahn simpleit vantage)

if [ "${1:-run}" = "summarize" ]; then
    exec python3 vantage/summarize.py "$OUT_BASE"
fi

mkdir -p "$OUT_BASE"
for rep in $(seq 1 "$REPS"); do
    for short in "${TRIO[@]}"; do
        out="$OUT_BASE/rep-$rep/$short"
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
        python3 -m wanbench.cli campaign \
            --config "configs/paper-n20-leader-relay-$short.yaml" \
            --out "$out" --execute \
            ${resume[@]+"${resume[@]}"} \
            2>&1 | tee -a "$OUT_BASE/rep-$rep-$short.log"
    done
done

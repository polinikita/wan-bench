#!/usr/bin/env bash
# Plateau anchors for the two Starfish-artifact baselines, each on its
# disclosed fleet (see knee.sh for the knee windows and fleet rationale):
#   Bluestreak   100 / 10k / 150k on m5d.2xlarge
#   Sailfish++   100 / 10k        on c5d.2xlarge
# Two repetitions by default; medians per cell, matching the knee cells.
#
#   ./anchors.sh             two repetitions, skipping completed ones
#   REPS=N ./anchors.sh      a different repetition count
#   ./anchors.sh summarize   median across repetitions per (variant, rate)
set -euo pipefail
cd "$(dirname "$0")/../.."

OUT_BASE=results/vantage/throughput-starfish-anchors
REPS="${REPS:-2}"
DUO=(bluestreak sailfishpp)

if [ "${1:-run}" = "summarize" ]; then
    exec python3 vantage/summarize.py "$OUT_BASE"
fi

mkdir -p "$OUT_BASE"
for rep in $(seq 1 "$REPS"); do
    for short in "${DUO[@]}"; do
        out="$OUT_BASE/rep-$rep/$short"
        status=$(python3 -c 'import json,sys
for f in ("campaign.json", "matrix.json"):
    try:
        print(json.load(open(sys.argv[1] + "/" + f)).get("status", "absent")); break
    except OSError: pass
else: print("absent")' "$out")
        case "$status" in
            completed|completed_with_failures)
                echo "knee.sh: $out is $status, skipping (delete it to rerun)"
                continue ;;
            absent) resume=() ;;
            *) echo "knee.sh: $out is '$status', resuming"; resume=(--resume) ;;
        esac
        python3 -m wanbench.cli campaign \
            --config "configs/paper-n100-$short-anchors.yaml" \
            --out "$out" --execute \
            ${resume[@]+"${resume[@]}"} \
            2>&1 | tee -a "$OUT_BASE/rep-$rep-$short.log"
    done
done

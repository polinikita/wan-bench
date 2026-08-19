#!/usr/bin/env bash
# Targeted knee windows for the two Starfish-artifact baselines with the
# payload-compacted image, each on its measured-best fleet:
#   Bluestreak   225k / 250k / 275k on m5d.2xlarge (storage-sensitive: c5d
#                caps it at ~161k with idle cores; m5d reproduces the
#                recorded m5d-gc knee)
#   Sailfish++   150k / 170k / 200k on c5d.2xlarge (clock-sensitive: 149.7k
#                at 150k on c5d vs 93.6k on m5d; c5d is the shared fleet)
# Two repetitions by default; the paper reports the median per cell and
# discloses the Bluestreak fleet exception.  Invalid prior attempts live in
# results/vantage/_invalid: pre-compaction full-ladder sweeps (OOM at 200k),
# the Bluestreak c5d attempt, and the Sailfish++ m5d attempt.
#
#   ./knee.sh             two repetitions (fresh fleet each), skipping done ones
#   REPS=N ./knee.sh      a different repetition count
#   ./knee.sh summarize   median across repetitions per (variant, rate)
set -euo pipefail
cd "$(dirname "$0")/../.."

OUT_BASE=results/vantage/throughput-starfish-knee
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
            --config "configs/paper-n100-$short-knee.yaml" \
            --out "$out" --execute \
            ${resume[@]+"${resume[@]}"} \
            2>&1 | tee -a "$OUT_BASE/rep-$rep-$short.log"
    done
done

#!/usr/bin/env bash
# Run the full 3x3 paper matrix (three questions x three repetitions) with a
# hard vCPU-accounting gate, so concurrent fleets never exceed the account
# quota. Each campaign is counted at its PEAK footprint for its whole
# lifetime (committee scaling reserves its n=100 stage from the start), so
# the gate is conservative and cannot overshoot mid-campaign.
#
#   ./run-all.sh                 schedule everything under VCPU_CAP (default 1800)
#   VCPU_CAP=976 ./run-all.sh    a tighter quota
#
# Completed repetitions (existing rep-N directories) are skipped, so an
# interrupted schedule resumes by rerunning the same command. Single AZ
# capacity errors surface as a failed campaign in its log; rerun to retry.
set -uo pipefail
cd "$(dirname "$0")/.."

VCPU_CAP="${VCPU_CAP:-1800}"
REPS="${REPS:-3}"

# question:config:peak_vcpus  (fleet+control at 8 vCPU each), longest first.
UNITS=(
    "throughput:configs/paper-n100-throughput.yaml:808"
    "committee-scaling:configs/paper-committee-scaling.yaml:808"
    "leader-relay:configs/n20-leader-relay-scaling.yaml:168"
)

declare -a run_pids run_cost run_name
used=0

launch() {
    local question=$1 config=$2 cost=$3 rep=$4
    local out="results/vantage/$question/rep-$rep"
    mkdir -p "results/vantage/$question"
    echo "run-all: launching $question rep-$rep (+$cost vCPU, $((used + cost))/$VCPU_CAP)"
    python3 -m wanbench.cli campaign --config "$config" --out "$out" --execute \
        > "results/vantage/$question/rep-$rep.log" 2>&1 &
    run_pids+=($!)
    run_cost+=("$cost")
    run_name+=("$question rep-$rep")
    used=$((used + cost))
}

reap() {
    local i
    for i in "${!run_pids[@]}"; do
        if ! kill -0 "${run_pids[$i]}" 2>/dev/null; then
            wait "${run_pids[$i]}" 2>/dev/null
            local rc=$?
            echo "run-all: ${run_name[$i]} finished (rc=$rc, -${run_cost[$i]} vCPU)"
            used=$((used - run_cost[$i]))
            unset "run_pids[$i]" "run_cost[$i]" "run_name[$i]"
        fi
    done
    run_pids=("${run_pids[@]+"${run_pids[@]}"}")
    run_cost=("${run_cost[@]+"${run_cost[@]}"}")
    run_name=("${run_name[@]+"${run_name[@]}"}")
}

# Pending queue in launch priority: all reps of the longest campaign lead, so
# the critical path starts immediately and shorter fleets fill the gaps.
pending=()
for unit in "${UNITS[@]}"; do
    IFS=: read -r question config cost <<< "$unit"
    for rep in $(seq 1 "$REPS"); do
        if [ -e "results/vantage/$question/rep-$rep" ]; then
            echo "run-all: $question rep-$rep exists, skipping"
        else
            pending+=("$question:$config:$cost:$rep")
        fi
    done
done

while [ "${#pending[@]}" -gt 0 ] || [ "${#run_pids[@]}" -gt 0 ]; do
    reap
    next=()
    for entry in "${pending[@]+"${pending[@]}"}"; do
        IFS=: read -r question config cost rep <<< "$entry"
        if [ $((used + cost)) -le "$VCPU_CAP" ]; then
            launch "$question" "$config" "$cost" "$rep"
        else
            next+=("$entry")
        fi
    done
    pending=("${next[@]+"${next[@]}"}")
    if [ "${#run_pids[@]}" -gt 0 ]; then
        sleep 60
    elif [ "${#pending[@]}" -gt 0 ]; then
        echo "run-all: nothing is running and no pending unit fits under" \
             "VCPU_CAP=$VCPU_CAP; aborting" >&2
        exit 1
    fi
done

echo "run-all: matrix complete"
for unit in "${UNITS[@]}"; do
    IFS=: read -r question _ _ <<< "$unit"
    echo; echo "== $question (median of repetitions)"
    python3 vantage/summarize.py "results/vantage/$question" || true
done

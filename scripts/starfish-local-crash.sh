#!/usr/bin/env bash
# Local crash-recovery gate for the Starfish-family protocols.
#
# Runs N `starfish run` processes on 127.0.0.1 (ports are index-derived, so one
# host is collision-free), SIGKILLs a set of authorities, restarts the same
# processes on their preserved RocksDB stores, and samples every node's
# sequenced_transactions_total and commit_index into a CSV once per second.
#
# `run` is the subcommand with real crash recovery; `dry-run` wipes storage on
# every boot and must not be used here. mimic_latency=true applies the
# protocol's internal AWS RTT table, so no tc/netem is needed locally.
#
#   scripts/starfish-local-crash.sh --consensus bluestreak
#   scripts/starfish-local-crash.sh --consensus sailfish-pp --nodes 10 \
#       --load 1000 --kill 7,8,9 --at 20 --down 20 --settle 60
set -euo pipefail

STARFISH_REPO="${STARFISH_REPO:-$HOME/code/starfish}"
CONSENSUS=""
N=10
LOAD_TOTAL=1000
TX_SIZE=512
KILL="7,8,9"
AT=20
DOWN=20
SETTLE=60
WORKDIR=""

usage() {
    cat >&2 <<'EOF'
usage: starfish-local-crash.sh --consensus NAME [--nodes N] [--load TOTAL_TPS]
                               [--kill i,j,...] [--at S] [--down S] [--settle S]
                               [--workdir DIR]

  --consensus  starfish --consensus value (e.g. bluestreak, sailfish-pp)
  --nodes      committee size                       (default 10)
  --load       aggregate tx/s, split evenly         (default 1000)
  --kill       authorities to SIGKILL together      (default 7,8,9)
  --at         seconds of healthy load before kill  (default 20)
  --down       seconds the victims stay down        (default 20)
  --settle     observation window after restart     (default 60)
  --workdir    genesis/storage/log directory        (default: mktemp -d)
EOF
    exit 2
}

while [ $# -gt 0 ]; do
    case "$1" in
        --consensus) CONSENSUS="$2"; shift 2 ;;
        --nodes)     N="$2"; shift 2 ;;
        --load)      LOAD_TOTAL="$2"; shift 2 ;;
        --kill)      KILL="$2"; shift 2 ;;
        --at)        AT="$2"; shift 2 ;;
        --down)      DOWN="$2"; shift 2 ;;
        --settle)    SETTLE="$2"; shift 2 ;;
        --workdir)   WORKDIR="$2"; shift 2 ;;
        -h|--help)   usage ;;
        *) echo "starfish-local-crash: unknown argument '$1'" >&2; usage ;;
    esac
done

[ -n "$CONSENSUS" ] || { echo "starfish-local-crash: --consensus is required" >&2; usage; }
[ $((LOAD_TOTAL % N)) -eq 0 ] || { echo "starfish-local-crash: --load must divide by --nodes" >&2; exit 2; }
LOAD_PER_NODE=$((LOAD_TOTAL / N))
[ -n "$WORKDIR" ] || WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/starfish-crash-$CONSENSUS.XXXX")"
mkdir -p "$WORKDIR/logs"

VICTIMS=()
IFS=, read -ra parts <<<"$KILL"
for i in "${parts[@]}"; do
    [ "$i" -lt "$N" ] || { echo "starfish-local-crash: victim $i out of range" >&2; exit 2; }
    VICTIMS+=("$i")
done

BIN="$STARFISH_REPO/target/release/starfish"
echo "starfish-local-crash: building starfish (jobs=4)"
(cd "$STARFISH_REPO" && CARGO_BUILD_JOBS=4 cargo build --release --bin starfish >/dev/null)

echo "starfish-local-crash: genesis for n=$N in $WORKDIR"
printf 'mimic_latency: true\n' > "$WORKDIR/node-parameters.yaml"
IPS=()
for ((i = 0; i < N; i++)); do IPS+=("127.0.0.1"); done
(cd "$WORKDIR" && "$BIN" benchmark-genesis --ips "${IPS[@]}" \
    --working-directory "$WORKDIR" \
    --node-parameters-path "$WORKDIR/node-parameters.yaml" >/dev/null)
cat > "$WORKDIR/parameters.yaml" <<EOF
load: $LOAD_PER_NODE
transaction_size: $TX_SIZE
transaction_mode: random
EOF

declare -a PIDS
spawn() {
    local i="$1"
    "$BIN" run --authority "$i" \
        --committee-path "$WORKDIR/committee.yaml" \
        --public-config-path "$WORKDIR/public-config.yaml" \
        --private-config-path "$WORKDIR/private-config-$i.yaml" \
        --parameters-path "$WORKDIR/parameters.yaml" \
        --consensus "$CONSENSUS" \
        >> "$WORKDIR/logs/node-$i.log" 2>&1 &
    PIDS[$i]=$!
}

cleanup() {
    for pid in "${PIDS[@]:-}"; do kill -9 "$pid" 2>/dev/null || true; done
    [ -n "${POLLER_PID:-}" ] && kill "$POLLER_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

CSV="$WORKDIR/samples.csv"
echo "ts_ms,node,sequenced,commit_index" > "$CSV"
poller() {
    while true; do
        local ts
        ts="$(python3 -c 'import time;print(int(time.time()*1000))')"
        for ((i = 0; i < N; i++)); do
            local port=$((1500 + N + i))
            # Samples may carry labels: `name{node="node-0"} value`.
            curl -fsS --max-time 1 "http://127.0.0.1:$port/metrics" 2>/dev/null |
                awk -v ts="$ts" -v n="$i" '
                    /^sequenced_transactions_total[{ ]/{seq=$NF}
                    /^commit_index[{ ]/{ci=$NF}
                    END{if (seq != "" || ci != "") printf "%s,%s,%s,%s\n", ts, n, seq, ci}' \
                >> "$CSV" || true
        done
        sleep 1
    done
}

echo "starfish-local-crash: spawning $N nodes ($CONSENSUS, ${LOAD_PER_NODE} tx/s each)"
for ((i = 0; i < N; i++)); do spawn "$i"; done
poller & POLLER_PID=$!

# Anchor the healthy window at first commit progress, not at process start:
# the generator waits initial_delay (~10 s) before offering load.
echo "starfish-local-crash: waiting for first sequenced transactions"
progressed() {
    # Numeric coercion keeps the CSV header from matching.
    tail -n "$N" "$CSV" 2>/dev/null |
        awk -F, '$3 + 0 > 0 {found=1} END {exit !found}'
}
for _ in $(seq 1 60); do
    if progressed; then break; fi
    sleep 1
done
progressed || {
    echo "starfish-local-crash: no progress after 60s; see $WORKDIR/logs" >&2; exit 1; }

echo "starfish-local-crash: healthy for ${AT}s"
sleep "$AT"
DOWN_MS="$(python3 -c 'import time;print(int(time.time()*1000))')"
echo "starfish-local-crash: SIGKILL authorities [${VICTIMS[*]}] for ${DOWN}s"
for i in "${VICTIMS[@]}"; do kill -9 "${PIDS[$i]}"; done
sleep "$DOWN"

echo "starfish-local-crash: restarting authorities [${VICTIMS[*]}] on their stores"
UP_MS="$(python3 -c 'import time;print(int(time.time()*1000))')"
for i in "${VICTIMS[@]}"; do spawn "$i"; done
sleep "$SETTLE"

python3 - "$CSV" "$DOWN_MS" "$UP_MS" "${VICTIMS[*]}" <<'PYEOF'
import csv, statistics, sys

path, down_ms, up_ms = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
victims = {int(v) for v in sys.argv[4].split()}
rows = [(int(r["ts_ms"]), int(r["node"]), float(r["sequenced"] or 0),
         float(r["commit_index"] or 0))
        for r in csv.DictReader(open(path)) if r["sequenced"] or r["commit_index"]]
if not rows:
    sys.exit("starfish-local-crash: no samples collected -- poller failure")

def rate(node, lo, hi):
    pts = sorted((ts, s) for ts, n, s, _ in rows if n == node and lo <= ts < hi)
    if len(pts) < 2 or pts[-1][0] == pts[0][0]:
        return None
    return (pts[-1][1] - pts[0][1]) / ((pts[-1][0] - pts[0][0]) / 1000)

def committee(lo, hi):
    vals = [r for n in {n for _, n, _, _ in rows} if (r := rate(n, lo, hi)) is not None]
    return statistics.median(vals) if vals else 0.0

end = max(ts for ts, *_ in rows)
print(f"pre-fault committee tx/s (median): {committee(down_ms - 15_000, down_ms):.0f}")
print(f"outage committee tx/s (median):    {committee(down_ms, up_ms):.0f}")
print(f"post-restart committee tx/s:       {committee(up_ms + 20_000, end):.0f}")

final = {}
for ts, n, _s, ci in sorted(rows):
    final[n] = ci
head = max(final.values())
for v in sorted(victims):
    lag = head - final.get(v, 0)
    status = "CAUGHT UP" if lag <= 50 else f"LAGGING by {lag:.0f}"
    print(f"restarted node {v}: commit_index {final.get(v, 0):.0f} vs head {head:.0f} -- {status}")
PYEOF

echo "starfish-local-crash: samples in $CSV, logs in $WORKDIR/logs"

# wan-bench

## Overview

`wan-bench` runs BFT protocol benchmarks on AWS EC2. It provisions one validator per
instance and one control instance for Prometheus and Grafana. Validators use
private IPs and run in one availability zone.

Supported adapters:

- Vantage, Autobahn, and Simple-IT binaries
- Starfish protocol variants

The harness supports load sweeps, `tc netem` WAN shaping, crash and network
faults, image digest pinning, incremental result checkpoints, and tag-scoped
teardown.

## Requirements

- Python 3.10+
- AWS credentials with EC2, STS, and related read permissions
- An EC2 key pair in the target region
- The matching local SSH private key
- System `ssh`, `scp`, and Git clients
- A pullable validator image, or a local Git repository for remote builds

Service Quotas access is optional. Without it, planning reports the required
vCPU count but cannot verify the account limit.

## Installation

```bash
python3 -m pip install -e .
```

Verify the command:

```bash
wanbench --help
```

## Quick Start

Copy [configs/example.yaml](configs/example.yaml), then set the AWS region, EC2
key pair, SSH key path, image, and workload.

Run one complete benchmark:

```bash
wanbench run --config configs/example.yaml --out results/example
```

Run an increasing load sweep:

```bash
wanbench sweep \
  --config configs/example.yaml \
  --out results/sweep \
  --rates 1000,2000,3000,4000
```

Rates are aggregate transactions per second. Each rate must be divisible by the
validator count. A sweep retries one failed point and stops after a material
throughput decrease. `--no-early-stop` records the full ladder, and
`--strict-through-rate` permits congestion above a specified load while keeping
lower points strict. `--min-offered-throughput-pct` stops before the next point
when committed throughput falls below the configured fraction of reachable load.
Normally reachable and offered load are identical. For an explicit fixed
Byzantine lane profile that omits every non-publisher and refuses repair, the
harness records the isolated authors' share as unreachable instead of treating
its expected absence as overload.

The current leader-relay experiment sweeps one uniform total workload. At
`n=20`, all validators receive the same input share; the six Byzantine authors'
shares traverse the normal data path but are reported separately from honest
goodput and latency. Each Byzantine lane deliberately forms one batch per
`Delta` and sends every batch only to a fixed `(f-1)=5`-wide correct-holder
group. Together with the author's local copy, this gives exactly `f=6` direct
holders, one below the `f+1=7` PoA threshold; the other Byzantine validators do
not receive the bytes. Groups are staggered across lanes, covering all 14
correct consensus leaders while each holder retains a complete lane prefix.
The `n=40` manifest applies the same construction with `f=13`, 12 correct
holders per Byzantine lane, and all 27 correct leaders covered. Headers remain
visible, and Byzantine authors refuse repair. A
Byzantine Autobahn consensus leader proposes its certified cut, avoiding a
separate self-inflicted timeout; an honest optimistic leader includes its
locally held tips and must relay their bytes before voting. Autobahn cars retain
ordinary payload capacity. Vantage and Simple-IT retain their normal lane
rules. The campaign uses private addresses, `c5d.2xlarge` instances, and the
AWS RTT matrix through `tc netem`. The `n=20` capacity ladder is the compact
100, 1k, 10k, 20k, 100k, and 200k TPS series. The `n=40` regression retains
the finer 80, 200, 400, 600, 800, 1k, 5k, 10k, 20k, 40k, 60k, 80k, 100k,
125k, 150k, 175k, 200k, and 250k series:

```bash
wanbench campaign --config configs/n20-leader-relay-scaling.yaml

# Execute each protocol on a fresh fleet. A terminal overload must not leave
# the next protocol dependent on SSH recovery from the previous one.
wanbench campaign --config configs/n20-leader-relay-scaling.yaml \
  --only autobahn-optimistic-a2a --out results/n20-leader-relay-optimistic --execute
wanbench campaign --config configs/n20-leader-relay-scaling.yaml \
  --only vantage --out results/n20-leader-relay-vantage --execute
wanbench campaign --config configs/n20-leader-relay-scaling.yaml \
  --only simpleit-optrbc --out results/n20-leader-relay-simpleit --execute

wanbench campaign --config configs/n40-leader-relay-scaling.yaml
wanbench campaign --config configs/n40-leader-relay-scaling.yaml --execute
```

The `n=20` capacity study treats every point as exploratory: it records replica
skew and Byzantine commitment without rejecting the point, and stops only after
a 20% throughput fall or when useful throughput is below 25% of reachable
offered load. The `n=40` manifest remains a strict regression through 10k: each
Optimistic point is retried and then fails unless at least 80% of the offered
Byzantine share appears in `committed_uncounted_tps_median`. At 1k total offered
load, the expected split is 700 honest and 300 Byzantine tx/s for `n=20`, and
675 honest and 325 Byzantine tx/s for `n=40`.

The older fixed-useful-load/background-payload diagnostic remains in
[`configs/n40-payload-drop-scaling.yaml`](configs/n40-payload-drop-scaling.yaml).

The prepared `n=100` Byzantine-lane campaign compares Autobahn optimistic
all-to-all, Vantage, and Simple-IT Opt-RBC. Its 33 Byzantine authors publish
lane data only inside their cohort and refuse certificate, header, and batch
repair to all 67 other validators, while consensus traffic stays live. It uses
private validator addresses, `c5d.2xlarge` instances, and the ten-region RTT
matrix through `tc netem`:

```bash
wanbench campaign --config configs/n100-data-lane-drop-scaling.yaml
wanbench campaign --config configs/n100-data-lane-drop-scaling.yaml --execute
```

## Campaigns

A campaign runs protocol variants sequentially. With `committee_sizes`, it
provisions each size in increasing order and tears down that fleet before
creating the next one.

Planning is read-only and creates no EC2 resources:

```bash
wanbench campaign --config configs/paper-committee-scaling.yaml
```

The plan verifies local files, AWS identity, the EC2 key pair, instance-type
availability, dashboard paths, and image access. Registry tags are resolved to
immutable digests.

Create the fleet and run the campaign only after reviewing the plan:

```bash
wanbench campaign --config configs/paper-committee-scaling.yaml --execute
```

Resume incomplete variants or select a subset:

```bash
wanbench campaign --config configs/paper-committee-scaling.yaml --execute --resume
wanbench campaign --config configs/paper-committee-scaling.yaml --execute --only vantage,bluestreak
```

Single-fleet state is checkpointed in `campaign.json`. A committee matrix uses
`matrix.json` and writes combined `points.csv`, `points.json`, and `README.md`.

## Commands

```text
up        provision and prepare a fleet
deploy    generate configuration and launch validators
collect   collect one measurement
down      terminate a run
run       execute up, deploy, collect, and down
sweep     measure an increasing rate ladder
campaign  plan or execute several variants on one fleet
fault     inject or clear a crash or network fault
nuke      terminate all Project=wan-bench resources in a region
```

`up` prints the Grafana URL. Grafana anonymous access is read-only and is limited
to the caller's public `/32` by default. Set `grafana_open_cidr: ""` to keep the
port closed.

## Configuration

Run configuration is defined by `RunConfig` in
[wanbench/config.py](wanbench/config.py). Important defaults include:

- `region: eu-west-1` (Ireland)
- `wan.mode: netem`
- `echo_avail_claims: true` for Vantage-family protocols
- `vantage_compact_ids: true` for Vantage
- `max_header_delay_ms: 100`
- `delta_ms: 200`; Autobahn derives a `10 * Delta` round timeout and the
  Simple-IT adapters derive `8 * Delta` (Opt-RBC) or `5 * Delta` (Bracha-RBC)
- `prometheus_scrape_interval_s: 30`
- `keep_monitoring_on_down: false`
- `ssh_open_cidr: null`, resolved to the caller's public `/32`
- `spot: false`

`image_source: registry` pulls a published image. `build-on-control` sends a Git
archive to the control instance, builds it there, and distributes it through a
private registry.

### Faults

`fault.at_s` counts from the moment the metrics-active window opens, so
`at_s: 20` injects 20 s into the measured window. `split` and `blip` clear
after `for_s`. `crash` kills the containers with SIGKILL; with `for_s > 0` the
same containers are restarted in place (`docker start`, state preserved) after
`for_s`, while `for_s: 0` keeps them down. A crash-restart cycle must satisfy
`at_s + for_s + 30 <= duration_s`. `wanbench fault restart --nodes i,j`
recovers crashed containers manually.

Fault runs write two extra artifacts next to `summary.json`:
`fault-timeline.json` (epoch-ms down/up timestamps) and `timeseries.json`
(Prometheus range queries over the whole window: committee median tx/s,
per-node rates, live-validator count, and per-protocol recovery gauges).
`summary.json` excludes the crash cohort from every median and lists it under
`excluded_nodes`; use the time series for the dip and recovery. The
`configs/crash-n40-*.yaml` files encode the paper's crash-restart scenario;
a 5 s `prometheus_scrape_interval_s` is needed to resolve the recovery curve.

## Safety

- Every resource is tagged with `Project=wan-bench` and `Run=<run-id>`.
- A run uses one availability zone and one validator instance type.
- Hosts receive a renewable automatic termination deadline.
- Normal teardown waits for termination and deletes the run security group.
- Setup and campaign failures run the same teardown path.
- `nuke` is a region-wide backstop for all `Project=wan-bench` resources.

Retaining the monitoring host is opt-in through
`keep_monitoring_on_down: true` or `down --keep-monitoring`. Release it with
`down --no-keep-monitoring` when finished.

## Results

Sweeps write `sweep.json`, `effective-config.yaml`, raw Prometheus snapshots, and
per-point summaries. Strict sweeps reject any validator below 80% of the
committee median. Campaigns also save bounded node diagnostics and a Prometheus
TSDB archive before teardown. Each completed campaign writes `monitoring/` with
an importable Grafana dashboard and a local Compose viewer:

```bash
docker compose -f results/example/monitoring/compose.yaml up
```

Archived series include stable run, variant, protocol, committee-size, offered-
rate, and reachable-rate labels.

`bandwidth_efficiency_p50` is the median validator's outbound wire bytes divided
by its sequenced transaction bytes, matching the Bluestreak committee-scaling
metric. The estimated non-payload value subtracts the `(n-1)/n` payload share;
it still includes framing, retries, and other control traffic.
For Vantage-family leader-relay runs, `committed_uncounted_tps_median` records
committed marker-2 Byzantine payload separately from useful `tps_median`.

Promote a sweep to a self-contained record with:

```bash
python3 -m wanbench.record --sweep results/example/sweep.json
```

Older sweeps without embedded configuration also require `--config`.

Promote a completed committee matrix to a compact tracked paper record:

```bash
python3 -m wanbench.record \
  --matrix results/example/matrix.json --config configs/example.yaml \
  --stamp 20260811
```

The record stores summaries and raw-archive checksums, not Prometheus databases.

Promote a finished single-committee campaign, including partial protocol
failures, with:

```bash
python3 -m wanbench.record \
  --campaign results/example/campaign.json --config configs/example.yaml \
  --stamp 20260811
```

The committee-scaling record used by the Vantage paper is in
[`recorded/paper-committee-scaling-20260811`](recorded/paper-committee-scaling-20260811).
The compact-identifier A/B record is in
[`recorded/vantage-compact-ids-local-ab-20260811`](recorded/vantage-compact-ids-local-ab-20260811).

The prepared `n=100` paper throughput campaign is
[`configs/paper-n100-throughput.yaml`](configs/paper-n100-throughput.yaml). It
uses seven variants on one `c5d.2xlarge` fleet in Ireland, private addresses,
netem, and the ten-region RTT matrix. Plan or execute it with one command:

```bash
wanbench campaign --config configs/paper-n100-throughput.yaml
wanbench campaign --config configs/paper-n100-throughput.yaml --execute
```

The 100 and 10,000 tx/s points are strict. Higher loads are exploratory, and a
variant stops when committed throughput falls below 95% of offered load or
drops by more than 5% from the previous point.

The measured paper record is in
[`recorded/paper-n100-throughput-20260811`](recorded/paper-n100-throughput-20260811).

The Starfish payload-compaction validation is in
[`recorded/paper-n100-starfish-m5d-gc-20260811`](recorded/paper-n100-starfish-m5d-gc-20260811).

The Vantage n=20 latency, traffic, and seal-route study is in
[`recorded/vantage-n20-pipeline-20260812`](recorded/vantage-n20-pipeline-20260812).

The n=20 Byzantine leader-relay capacity comparison is in
[`recorded/n20-leader-relay-20260815`](recorded/n20-leader-relay-20260815).

## Tests

Run the offline regression suite:

```bash
python3 -m unittest discover -s tests -v
```

## Repository Layout

```text
wanbench/    AWS lifecycle, deployment, collection, campaigns, and CLI
configs/     examples and the paper campaign
latency/     WAN RTT matrices
tests/       offline regression tests
recorded/    compact reproducibility records
```

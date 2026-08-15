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
`n=100`, all validators receive the same input share; the 33 Byzantine authors'
shares traverse the normal data path but are excluded from honest goodput and
latency. Each Byzantine author deliberately forms one batch per `Delta` and
sends it only to an `(f-1)`-wide rotating correct group. Together with the
author's local copy, this gives exactly `f` direct holders, one below the `f+1`
PoA threshold; the other Byzantine validators do not receive the bytes. All
authors retain that group for five batches, then advance it by `f-1`; every
correct leader is covered by the disclosed `5 Delta = 1 s` epochs and holds all
faulty lanes while selected. Headers remain visible, and Byzantine authors
refuse repair. When one of those publishers is the Autobahn consensus
leader it proposes its certified cut, avoiding a separate self-inflicted
timeout; honest leaders retain the optimistic relay path. For Autobahn,
selected Byzantine cars are capped at one digest, keeping one sub-PoA tip
active until later dissemination supplies another holder. Each targeted
optimistic proposer must relay its locally held tips. The other protocols retain their normal lane
rules. The campaign uses private addresses, `c5d.2xlarge` instances, and the
AWS RTT matrix through `tc netem`:

```bash
wanbench campaign --config configs/n100-leader-relay-scaling.yaml
wanbench campaign --config configs/n100-leader-relay-scaling.yaml --execute
```

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

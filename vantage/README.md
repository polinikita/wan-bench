# Vantage paper experiments

One subfolder per experimental question, one runner each. Every runner executes
the question's campaign **three times** (fresh fleet per repetition) and the
paper reports the **median across the three repetitions** per cell; the spread
is kept visible so instability is never averaged away.

| question | folder | config | fleet |
|---|---|---|---|
| Throughput and latency versus the baselines at n=100 | `throughput/` | `configs/paper-n100-throughput.yaml` | 100+1 × c5d.2xlarge |
| Latency, CPU, and wire cost as the committee grows | `committee-scaling/` | `configs/paper-committee-scaling.yaml` | up to 100+1 × c5d.2xlarge |
| Behavior under leader-relay faults at n=20 | `leader-relay/` | `configs/n20-leader-relay-scaling.yaml` | 20+1 × c5d.2xlarge |

## Prerequisites

- An AWS account with quota for the fleet sizes above in `eu-west-1`
  (single availability zone; the configs pin `eu-west-1a`).
- Credentials in the environment (`aws sts get-caller-identity` must work),
  a key pair named in the config (`key_name` / `ssh_key_path` — edit these
  two fields to your own), and Python 3.12+ with the repo's requirements.
- Nothing needs to be built: the configs pin the exact node image by digest
  on ghcr.io, and the harness verifies the manifest **before** provisioning.

## Running

```
./vantage/throughput/run.sh            # three repetitions, sequential fleets
REPS=1 ./vantage/throughput/run.sh     # a single repetition
./vantage/throughput/run.sh summarize  # median-of-repetitions table
```

Repetitions land in `results/vantage/<question>/rep-N/`; a repetition that
already exists is skipped, so an interrupted sequence resumes by rerunning the
same command. Fleets are provisioned per repetition and torn down afterwards;
never leave a run unattended without the deadman timer in the config.

`./vantage/run-all.sh` schedules the whole three-by-three matrix under a
vCPU quota (`VCPU_CAP`, default 1800): each campaign is reserved at its peak
footprint (an n=100 fleet is 808 vCPUs), throughput repetitions lead because
they are the critical path, and shorter fleets fill the remaining headroom.
Under 1800 vCPUs two n=100 fleets run side by side with leader relay in the
gap, and the full matrix completes in roughly two throughput repetitions of
wall time. Expect occasional single-AZ capacity stalls above two hundred
concurrent instances; a failed repetition is rerun by invoking the script
again.

## Instance sizing (measured, not guessed)

Every question runs on c5d.2xlarge (8 vCPU / 16 GB), so all figures share one
platform. The sizing is dictated by the measured load, not caution: at
250-275k tx/s the median node needs 3.0-3.2 cores with peaks above 4
(sailfish-pp needs 4.2 cores at 150k already, vantage itself reaches 3.6 at
250k) and up to 7.1 GiB of memory (autobahn-seamless at 275k). Lighter
questions would fit 4 vCPU, but nonuniform hardware would let a reviewer
attribute cross-protocol differences to provisioning.

## Metrics

Each repetition stores, per variant and rate point: `sweep.json` and
`points.csv` (headline numbers), per-node `baseline-/final-node-*.prom`
scrapes (every Prometheus series, including the `utilization_timer` core
sections and the `in_*` inbound families), `final-diagnostics-node-*.txt`
(docker stats and TCP socket state), and an archived Prometheus TSDB per
campaign for time-resolved inspection. `summarize.py` reduces sweep.json
across repetitions; everything deeper reads the raw scrapes.

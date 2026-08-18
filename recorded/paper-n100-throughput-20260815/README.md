# paper-n100-throughput

Status: completed. Started 2026-08-15T19:26:20.633959+00:00; finished 2026-08-15T19:58:55.343421+00:00.

| variant | offered tx/s | reachable tx/s | committed tx/s | % reachable | p50 ms | p99 ms | CPU cores/node | RSS MB/node | wire MB/s/node | healthy | outcome |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| vantage | 100 | 100 | 99.9 | 99.9 | 464.5 | 618.5 | 1.114 | 722.9 | 0.70 | 100/100 | accepted |
| vantage | 10,000 | 10,000 | 9,991.9 | 99.9 | 465.0 | 623.5 | 1.552 | 1385.5 | 6.04 | 100/100 | accepted |
| vantage | 150,000 | 150,000 | 149,980.1 | 100.0 | 488.0 | 657.0 | 2.667 | 2623.6 | 78.17 | 100/100 | accepted |
| vantage | 200,000 | 200,000 | 200,041.9 | 100.0 | 496.0 | 666.0 | 3.017 | 3042.0 | 103.91 | 100/100 | accepted |
| vantage | 225,000 | 225,000 | 222,339.9 | 98.8 | 538.5 | 1155.0 | 3.284 | 3373.7 | 116.79 | 100/100 | accepted |
| vantage | 250,000 | 250,000 | 174,054.6 | 69.6 | 561.0 | 1198.0 | 3.052 | 3786.8 | 104.33 | 100/100 | overloaded |

## Variant failures

None.

## Record

This directory contains the measured point summaries, exact campaign definition, pinned image digests, fleet provenance, and per-variant sweep records.

- `points.csv`: plot-ready table
- `points.json`: complete point records
- `measurements.json`: campaign, per-variant sweeps, and raw archive checksums
- `campaign.json`: execution status and effective configurations
- `config.yaml`: source campaign

Raw Prometheus databases are not stored in Git. Their paths, sizes, and SHA-256 digests are recorded in `measurements.json`.

# paper-n100-vantage-high-confirmation

Status: completed. Started 2026-08-15T20:11:13.095457+00:00; finished 2026-08-15T20:20:53.241251+00:00.

| variant | offered tx/s | reachable tx/s | committed tx/s | % reachable | p50 ms | p99 ms | CPU cores/node | RSS MB/node | wire MB/s/node | healthy | outcome |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| vantage | 250,000 | 250,000 | 231,539.6 | 92.6 | 509.5 | 683.5 | 3.492 | 3768.6 | 128.79 | 100/100 | overloaded |

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

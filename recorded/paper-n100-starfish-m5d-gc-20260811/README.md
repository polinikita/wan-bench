# paper-n100-starfish-m5d-gc

Status: completed. Started 2026-08-11T21:10:47.017249+00:00; finished 2026-08-11T21:44:50.143442+00:00.

| variant | offered tx/s | committed tx/s | p50 ms | p99 ms | CPU cores/node | RSS MB/node | wire MB/s/node | healthy | outcome |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---|
| bluestreak | 200,000 | 197,720.7 | 522.5 | 870.5 | 2.279 | 1778.8 | 103.53 | 100/100 | accepted |
| bluestreak | 225,000 | 222,219.7 | 529.5 | 942.8 | 2.564 | 2621.0 | 116.32 | 100/100 | accepted |
| bluestreak | 250,000 | 239,605.4 | 540.8 | 1478.8 | 2.800 | 4255.1 | 127.94 | 100/100 | accepted |
| bluestreak | 275,000 | 242,023.2 | 5661.7 | 18985.1 | 2.433 | 5428.4 | 131.23 | 100/100 | overloaded |
| sailfish-pp | 200,000 | 109,950.0 | 1079.7 | 54454.0 | 2.225 | 3599.8 | 65.39 | 100/100 | overloaded |

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

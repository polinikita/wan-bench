# paper-committee-scaling

Committees: 10, 20, 50, 100. Status: completed.

| n | variant | committed tx/s | p50 ms | CPU cores/node | wire MB/s/node | wire B/sequenced B | non-payload B/tx |
|---:|---|---:|---:|---:|---:|---:|---:|
| 10 | vantage | 100.1 | 447.5 | 0.057 | 0.160 | 3.195 | 1175.1 |
| 10 | autobahn-optimistic-a2a | 100.0 | 482.5 | 0.423 | 0.160 | 3.083 | 1117.9 |
| 10 | autobahn-seamless | 100.1 | 712.0 | 0.402 | 0.160 | 3.128 | 1140.6 |
| 10 | simpleit-optrbc | 100.0 | 639.0 | 0.045 | 0.280 | 5.449 | 2329.2 |
| 10 | simpleit-bracha | 99.9 | 765.5 | 0.045 | 0.280 | 5.424 | 2316.4 |
| 10 | bluestreak | 100.6 | 462.9 | 0.033 | 0.090 | 1.775 | 447.8 |
| 10 | sailfish-pp | 99.9 | 682.2 | 0.074 | 0.160 | 3.110 | 1131.7 |
| 20 | vantage | 100.0 | 436.0 | 0.098 | 0.280 | 5.417 | 2287.1 |
| 20 | autobahn-optimistic-a2a | 100.0 | 485.0 | 0.506 | 0.300 | 5.902 | 2535.4 |
| 20 | autobahn-seamless | 100.0 | 718.5 | 0.411 | 0.330 | 6.417 | 2798.9 |
| 20 | simpleit-optrbc | 100.1 | 669.0 | 0.081 | 0.870 | 17.002 | 8218.7 |
| 20 | simpleit-bracha | 100.0 | 785.0 | 0.073 | 0.870 | 16.981 | 8207.8 |
| 20 | bluestreak | 99.9 | 467.2 | 0.049 | 0.140 | 2.784 | 939.0 |
| 20 | sailfish-pp | 99.9 | 711.4 | 0.195 | 0.470 | 9.130 | 4188.1 |
| 50 | vantage | 99.9 | 448.0 | 0.269 | 0.640 | 12.476 | 5885.8 |
| 50 | autobahn-optimistic-a2a | 100.0 | 486.5 | 0.578 | 1.130 | 22.019 | 10771.8 |
| 50 | autobahn-seamless | 100.1 | 731.5 | 0.574 | 1.100 | 21.413 | 10461.7 |
| 50 | simpleit-optrbc | 100.0 | 666.5 | 0.228 | 4.850 | 94.776 | 48023.6 |
| 50 | simpleit-bracha | 100.1 | 778.0 | 0.224 | 4.850 | 94.639 | 47953.5 |
| 50 | bluestreak | 100.0 | 473.2 | 0.138 | 0.290 | 5.678 | 2405.6 |
| 50 | sailfish-pp | 99.9 | 788.6 | 0.724 | 2.310 | 45.184 | 22632.3 |
| 100 | vantage | 100.0 | 451.5 | 0.783 | 1.220 | 23.896 | 11727.9 |
| 100 | autobahn-optimistic-a2a | 100.0 | 500.5 | 1.061 | 3.290 | 64.343 | 32436.6 |
| 100 | autobahn-seamless | 100.1 | 748.0 | 0.952 | 3.330 | 65.159 | 32854.4 |
| 100 | simpleit-optrbc | 100.1 | 702.0 | 0.761 | 18.780 | 366.496 | 187138.9 |
| 100 | simpleit-bracha | 100.0 | 783.5 | 0.781 | 18.790 | 366.856 | 187323.5 |
| 100 | bluestreak | 100.0 | 479.9 | 0.286 | 0.530 | 10.437 | 4837.0 |
| 100 | sailfish-pp | 100.0 | 796.5 | 2.297 | 8.630 | 168.548 | 85789.8 |

## Fleet status

- n=10: completed
- n=20: completed
- n=50: completed
- n=100: completed

## Record

This directory contains the complete summary measurements and exact campaign definitions used for the paper sweep.

- `points.csv`: plot-ready table
- `points.json`: plot-ready structured rows
- `measurements.json`: per-point details, fleet provenance, and image digests
- `matrix.json`: execution status and definitions
- `config.yaml`: source campaign, when supplied

Raw Prometheus databases are not stored in Git. Their paths, sizes, and SHA-256 digests are recorded in `measurements.json`.

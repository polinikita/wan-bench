# paper-n100-throughput

Status: completed_with_failures. Started 2026-08-11T16:04:29.503717+00:00; finished 2026-08-11T19:38:14.188019+00:00.

| variant | offered tx/s | committed tx/s | p50 ms | p99 ms | CPU cores/node | RSS MB/node | wire MB/s/node | healthy | outcome |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---|
| vantage | 100 | 98.9 | 476.0 | 1228.0 | 0.869 | 1071.0 | 0.73 | 100/100 | accepted |
| vantage | 10,000 | 9,982.8 | 446.0 | 617.5 | 1.308 | 1483.9 | 6.09 | 100/100 | accepted |
| vantage | 150,000 | 149,781.9 | 460.5 | 634.0 | 2.330 | 2382.2 | 78.23 | 100/100 | accepted |
| vantage | 200,000 | 198,175.5 | 496.0 | 1101.0 | 2.631 | 2651.1 | 103.99 | 100/100 | accepted |
| vantage | 225,000 | 224,733.8 | 467.0 | 640.5 | 2.930 | 3143.8 | 116.89 | 100/100 | accepted |
| vantage | 250,000 | 239,373.6 | 471.0 | 646.0 | 3.153 | 3290.8 | 129.65 | 100/100 | accepted |
| vantage | 275,000 | 235,070.5 | 473.0 | 647.0 | 3.168 | 3574.9 | 131.56 | 100/100 | overloaded |
| autobahn-optimistic-a2a | 100 | 100.3 | 494.5 | 712.5 | 1.057 | 634.6 | 3.40 | 100/100 | accepted |
| autobahn-optimistic-a2a | 10,000 | 9,999.8 | 496.0 | 709.5 | 1.459 | 1022.9 | 8.77 | 100/100 | accepted |
| autobahn-seamless | 100 | 100.1 | 754.0 | 1047.5 | 0.905 | 637.2 | 3.33 | 100/100 | accepted |
| autobahn-seamless | 10,000 | 10,001.2 | 749.0 | 1053.0 | 1.301 | 1010.0 | 8.68 | 100/100 | accepted |
| autobahn-seamless | 150,000 | 149,966.4 | 772.5 | 1079.0 | 2.270 | 1618.7 | 80.81 | 100/100 | accepted |
| autobahn-seamless | 200,000 | 199,785.7 | 783.0 | 1091.0 | 2.577 | 1868.6 | 106.57 | 100/100 | accepted |
| autobahn-seamless | 225,000 | 224,970.2 | 787.5 | 1095.0 | 2.807 | 2169.7 | 119.46 | 100/100 | accepted |
| autobahn-seamless | 250,000 | 245,110.6 | 801.0 | 2086.0 | 2.981 | 2892.5 | 132.71 | 100/100 | accepted |
| autobahn-seamless | 275,000 | 205,301.4 | 803.0 | 1157.0 | 2.923 | 5065.1 | 137.91 | 100/100 | overloaded |
| simpleit-optrbc | 100 | 99.6 | 702.5 | 864.0 | 0.757 | 413.4 | 18.82 | 100/100 | accepted |
| simpleit-optrbc | 10,000 | 9,999.0 | 693.5 | 886.5 | 1.194 | 905.1 | 24.18 | 100/100 | accepted |
| simpleit-optrbc | 150,000 | 150,030.0 | 707.5 | 909.0 | 2.272 | 1687.2 | 96.37 | 100/100 | accepted |
| simpleit-optrbc | 200,000 | 199,437.1 | 710.5 | 916.0 | 2.592 | 1952.0 | 122.18 | 100/100 | accepted |
| simpleit-optrbc | 225,000 | 224,970.3 | 712.0 | 910.5 | 2.877 | 2462.8 | 135.04 | 100/100 | accepted |
| simpleit-optrbc | 250,000 | 240,496.3 | 714.0 | 915.5 | 3.025 | 2463.9 | 147.52 | 100/100 | accepted |
| simpleit-optrbc | 275,000 | 231,810.1 | 716.0 | 922.0 | 2.956 | 2743.4 | 146.80 | 100/100 | overloaded |
| simpleit-bracha | 100 | 99.7 | 777.0 | 994.0 | 0.774 | 413.3 | 18.83 | 100/100 | accepted |
| simpleit-bracha | 10,000 | 9,998.1 | 779.5 | 981.5 | 1.198 | 879.7 | 24.18 | 100/100 | accepted |
| simpleit-bracha | 150,000 | 149,982.7 | 792.5 | 999.5 | 2.266 | 1736.8 | 96.40 | 100/100 | accepted |
| simpleit-bracha | 200,000 | 200,123.0 | 795.5 | 1005.0 | 2.583 | 2023.5 | 122.19 | 100/100 | accepted |
| simpleit-bracha | 225,000 | 224,854.1 | 798.5 | 1007.0 | 2.868 | 2288.2 | 135.10 | 100/100 | accepted |
| simpleit-bracha | 250,000 | 242,316.2 | 799.5 | 1013.0 | 3.072 | 2522.5 | 148.02 | 100/100 | accepted |
| simpleit-bracha | 275,000 | 231,071.7 | 804.0 | 1025.0 | 2.964 | 2894.0 | 145.82 | 100/100 | overloaded |
| bluestreak | 100 | 100.0 | 477.3 | 639.9 | 0.320 | 310.0 | 0.54 | 100/100 | accepted |
| bluestreak | 10,000 | 10,010.6 | 481.0 | 641.2 | 0.397 | 956.2 | 5.69 | 100/100 | accepted |
| bluestreak | 150,000 | 148,968.6 | 514.3 | 790.2 | 1.597 | 6660.9 | 77.77 | 100/100 | accepted |
| bluestreak | 200,000 | 119,803.2 | 1133.7 | 14362.7 | 1.625 | 15571.6 | 74.16 | 61/100 | overloaded |
| sailfish-pp | 100 | 99.8 | 786.6 | 1916.0 | 2.868 | 671.8 | 8.71 | 100/100 | accepted |
| sailfish-pp | 10,000 | 10,010.7 | 789.3 | 1763.4 | 2.926 | 1551.2 | 13.83 | 100/100 | accepted |
| sailfish-pp | 150,000 | 149,874.6 | 843.8 | 2913.2 | 4.230 | 10064.3 | 86.08 | 100/100 | accepted |
| sailfish-pp | 200,000 | 57,600.0 | 1135.5 | 9895.2 | 4.333 | 15531.6 | 33.13 | 58/100 | overloaded |

## Variant failures

- `autobahn-optimistic-a2a`: RuntimeError: node(s) [14, 19, 39] were not committing after 175s at the progress barrier (not relaunched individually by design)

## Record

This directory contains the measured point summaries, exact campaign definition, pinned image digests, fleet provenance, and per-variant sweep records.

- `points.csv`: plot-ready table
- `points.json`: complete point records
- `measurements.json`: campaign, per-variant sweeps, and raw archive checksums
- `campaign.json`: execution status and effective configurations
- `config.yaml`: source campaign

Raw Prometheus databases are not stored in Git. Their paths, sizes, and SHA-256 digests are recorded in `measurements.json`.

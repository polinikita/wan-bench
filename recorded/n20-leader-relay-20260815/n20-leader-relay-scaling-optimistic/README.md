# autobahn-optimistic n=20 load sweep

## Provenance

- `image`: ghcr.io/polinikita/vantage-node@sha256:e4bc05f09ee916c4fd77fdc8cb6fec00009e67bcdab50529667f829152db756c
- `instance_type`: c5d.2xlarge
- `region`: eu-west-1
- `protocol`: autobahn-optimistic
- `nodes`: 20
- `tx_size`: 512
- `delta_ms`: 200
- `spot`: false
- warmup / window: 30 s / 120 s per point
- rate ladder: 100, 1,000, 10,000, 20,000, 100,000, 200,000
- early-stop threshold: 20.0% committed-TPS drop

## Points

| offered tx/s | reachable tx/s | % reachable | adversarial tx/s | committed tx/s (median) | committed tx/s (min) | ord p50 ms | ord p95 ms | ord p99 ms | mat p50 ms | mat p99 ms | CPU cores p50/node | wire MB/s p50/node | wire B / sequenced B | non-payload B/tx | healthy |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 100 | 70 | 99.9 | 0 | 70 | 70 | 811 | 1082 | 1164 | 817 | 1172 | 0.70 | 0.41 | 11.56 | 5431 | 20/20 |
| 1,000 | 700 | 100.0 | 0 | 700 | 699 | 816 | 1075 | 1158 | 823 | 1164 | 0.74 | 0.76 | 2.11 | 595 | 20/20 |
| 10,000 | 7,000 | 100.3 | 0 | 7,024 | 7,019 | 1584 | 2188 | 2344 | 1594 | 2352 | 0.78 | 5.80 | 1.61 | 339 | 20/20 |
| 20,000 | 14,000 | 101.2 | 0 | 14,169 | 14,058 | 1866 | 2646 | 2836 | 1878 | 2846 | 0.86 | 11.60 | 1.60 | 334 | 20/20 |
| 100,000 | 70,000 | 99.2 | 0 | 69,451 | 69,411 | 1922 | 2758 | 2926 | 1990 | 2972 | 1.33 | 68.31 | 1.92 | 497 | 20/20 |
| 200,000 | 140,000 | 76.4 | 0 | 106,950 | 104 | 4512 | 12287 | 21998 | 5350 | 23353 | 2.21 | 103.02 | 2.86 | 976 | 20->19 of 20 |

## Outcome

- status: **completed**
- ran the full rate ladder without early stop
- wall clock: 1648 s

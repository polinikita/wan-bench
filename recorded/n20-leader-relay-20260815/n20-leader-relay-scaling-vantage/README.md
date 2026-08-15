# vantage n=20 load sweep

## Provenance

- `image`: ghcr.io/polinikita/vantage-node@sha256:e4bc05f09ee916c4fd77fdc8cb6fec00009e67bcdab50529667f829152db756c
- `instance_type`: c5d.2xlarge
- `region`: eu-west-1
- `protocol`: vantage
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
| 100 | 70 | 100.0 | 0 | 70 | 70 | 444 | 878 | 1434 | 450 | 1441 | 0.12 | 0.20 | 5.60 | 2383 | 20/20 |
| 1,000 | 700 | 100.0 | 0 | 700 | 700 | 441 | 877 | 1434 | 446 | 1440 | 0.14 | 0.68 | 1.89 | 482 | 20/20 |
| 10,000 | 7,000 | 100.0 | 0 | 6,998 | 6,993 | 445 | 826 | 1364 | 451 | 1372 | 0.18 | 5.13 | 1.43 | 246 | 20/20 |
| 20,000 | 14,000 | 100.0 | 0 | 14,004 | 13,986 | 454 | 942 | 1382 | 459 | 1392 | 0.23 | 10.07 | 1.40 | 233 | 20/20 |
| 100,000 | 70,000 | 100.0 | 0 | 69,965 | 69,929 | 444 | 893 | 1345 | 450 | 1357 | 0.51 | 49.59 | 1.38 | 222 | 20/20 |
| 200,000 | 140,000 | 100.0 | 0 | 139,982 | 139,859 | 446 | 886 | 1308 | 454 | 1330 | 0.87 | 99.00 | 1.38 | 221 | 20/20 |

## Outcome

- status: **completed**
- ran the full rate ladder without early stop
- wall clock: 1354 s

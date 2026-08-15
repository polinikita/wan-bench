# simple-it n=20 load sweep

## Provenance

- `image`: ghcr.io/polinikita/vantage-node@sha256:e4bc05f09ee916c4fd77fdc8cb6fec00009e67bcdab50529667f829152db756c
- `instance_type`: c5d.2xlarge
- `region`: eu-west-1
- `protocol`: simple-it
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
| 100 | 70 | 100.0 | 0 | 70 | 70 | 665 | 810 | 862 | 671 | 868 | 0.08 | 0.90 | 25.12 | 12373 | 20/20 |
| 1,000 | 700 | 99.9 | 0 | 700 | 699 | 664 | 808 | 864 | 670 | 870 | 0.12 | 1.38 | 3.85 | 1485 | 20/20 |
| 10,000 | 7,000 | 100.1 | 0 | 7,006 | 6,985 | 671 | 816 | 870 | 677 | 876 | 0.16 | 5.83 | 1.63 | 346 | 20/20 |
| 20,000 | 14,000 | 99.9 | 0 | 13,993 | 13,979 | 676 | 824 | 880 | 682 | 886 | 0.21 | 10.77 | 1.50 | 283 | 20/20 |
| 100,000 | 70,000 | 100.0 | 0 | 70,031 | 69,980 | 668 | 812 | 868 | 674 | 875 | 0.48 | 50.28 | 1.40 | 232 | 20/20 |
| 200,000 | 140,000 | 100.0 | 0 | 140,038 | 139,997 | 670 | 814 | 870 | 676 | 876 | 0.86 | 99.71 | 1.39 | 226 | 20/20 |

## Outcome

- status: **completed**
- ran the full rate ladder without early stop
- wall clock: 1355 s

# Vantage compact-ID local A/B

This record measures Vantage's default one-byte committee identifiers against
the legacy 32-byte identifiers. Each release-binary run used 10 or 20
validators, one worker per validator, 100 random 512-byte transactions/s, the
default ten-region RTT matrix, a 60-second client interval, and the standard
1.32-second drain. Every run sequenced all 6,000 submitted transactions.

The paper metric is the median validator's outbound wire bytes divided by its
sequenced transaction bytes. Compact IDs reduce it from 3.2043 to 2.3212 at
`n=10`, and from 5.4861 to 3.6221 at `n=20`. Materialization latency changes by
at most 1.5 ms.

The projection fits `wire_efficiency = a + b(n - 1)` to the two local compact
measurements. As a check, the same fit over the two legacy controls predicts
the archived AWS legacy results at `n=50` and `n=100` within 1.2%. The compact
values at those sizes are projections, not measurements.

Source: Vantage commit `cfb730e9e5dc64a601d2e2f104d4bcd7aa65e5ae`.
The archived AWS controls are in `../paper-committee-scaling-20260811`.

Command template:

```sh
./target/release/node local-benchmark \
  --nodes N --workers 1 --rate 100 --tx-size 512 \
  --protocol vantage --duration 60 --data-dir PATH [--no-compact-ids]
```


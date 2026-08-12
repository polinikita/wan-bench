# Vantage n=20 pipeline study

This record explains Vantage's latency and traffic at 20 validators and 100
random 512-byte transactions/s. Each release-binary run lasted 60 seconds and
sequenced all 6,000 submitted transactions.

Three runs use the paper's ten-region AWS RTT matrix. Three controls replace
only that matrix with a uniform 143 ms inter-validator RTT, the median of the
off-diagonal entries after the matrix is repeated over 20 validators. The
matrix ranges from 1 to 309 ms. Both models apply half the RTT in each
direction without jitter.

| RTT model | order p50 | materialized p50 | proposal to seal p50 | fast seals |
|---|---:|---:|---:|---:|
| AWS matrix | 438 ms | 444 ms | 182 ms | 70.3% |
| Uniform 143 ms | 348 ms | 354 ms | 81 ms | 100% |

Values are medians of three run-level results. With heterogeneous delays,
READY quorums can reach a validator before the slowest ECHO, so about 30% of
local decisions use Direct AGB's READY route. With equal link delays, all ECHOs
arrive before the extra READY hop and every observed decision uses fast seal.

For the representative median matrix run, a transaction waits 11 ms to enter
a worker batch, an own block waits 51 ms from its first digest to publication,
and publication-to-order takes 368 ms at p50. Materializing the ordered bytes
adds 6 ms. These intervals describe different populations and are not
additive. The same run sends 24,876 logical unicast messages/s committee-wide.
AGB is 38.4% of typed serialized bytes, payload dissemination 29.2%, data
blocks 19.6%, state sync 5.4%, primary-worker traffic 4.3%, control 2.9%, and
all repair and replay categories together 0.2%.

The pipeline tracing feature is excluded from normal builds. Traced builds
bound block timing state at 4,096 entries per validator. A matched 30-second
A/B check sequenced 3,000/3,000 transactions in both builds; ordering p50 was
438 ms without tracing and 437 ms with tracing.

Source: Vantage commit `097784337333954dd51692cee71a32e711e0e3cb`.
Traced binary SHA-256:
`7dd77a040cd8529dd0a94b7daedca058533c7de108f00d9962fe22efd31132c1`.
Host: Apple M4 Pro, 14 logical CPUs, 48 GiB RAM, macOS 15.7.5.

Build and run:

```sh
cargo build --release -p node --features pipeline-tracing

./target/release/node local-benchmark \
  --nodes 20 --workers 1 --rate 100 --tx-size 512 \
  --protocol vantage --duration 60 --data-dir PATH

./target/release/node local-benchmark \
  --nodes 20 --workers 1 --rate 100 --tx-size 512 \
  --protocol vantage --duration 60 --data-dir PATH \
  --mimic-latency-ms 143
```

`measurements.json` contains every run summary. The logs retain the exact
benchmark output, including per-node route counts and message-type counters.

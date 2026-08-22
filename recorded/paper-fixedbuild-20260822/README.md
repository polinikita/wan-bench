# paper-fixedbuild-20260822 — figure data for the signature-free paper

The complete measurement record behind `figures/{committee-scaling,throughput-sweep,
throughput-sweep-overload,leader-relay}.dat` as regenerated on 2026-08-22 by
`vantage/gen_paper_dat.py` (the `.dat` files and their per-cell `provenance.json`
are under `figures/`; the contributing `sweep.json`/`campaign.json`/`matrix.json`
trees are under `measurements/`, without per-node scrapes or Prometheus archives).

## Images

| image | role |
|---|---|
| `ghcr.io/polinikita/vantage-node@sha256:d8e573c9…fac2289` (Vantage `fb4a0070`, main) | the reported build: all five shared-binary variants, every campaign under `measurements/vantage/` |
| `ghcr.io/polinikita/vantage-node@sha256:a8497560…9a58bd95` (Vantage `175c0ba`) | source of three reused cells (below), reported under the `d8e573c9` build |
| `ghcr.io/iotaledger/starfish-node@sha256:3f7ee52d…89d04d88` | Bluestreak and Sailfish++ (separate artifact) |

## Repetitions per figure

- **Q1 committee scaling** (`measurements/vantage/committee-scaling/rep-{1,2,3}`):
  three repetitions, seven variants, n ∈ {10, 20, 50}, all on c5d.2xlarge. The
  n=100 cell joins from the throughput ladder's rate-100 medians.
- **Q4 leader relay** (`measurements/vantage/leader-relay/rep-{1,2,3}`): three
  repetitions, three variants, six rates, n=20 c5d.2xlarge.
- **Q3 throughput ladder** (`measurements/vantage/throughput/rep-{1,2,3}`):
  vantage and autobahn-optimistic-a2a have three full-ladder repetitions;
  autobahn-seamless three repetitions through 250k and two at 275k;
  simpleit-optrbc and simpleit-bracha two repetitions through 250k (rep-3 was
  stopped by the operator after Q1/Q4 completed). Bluestreak's ladder comes from
  the m5d.2xlarge side campaigns (`throughput-starfish-anchors`, `…-knee`,
  including the imported m5d-gc 200k cell); Sailfish++ from the c5d side
  campaigns. Bluestreak is measured on m5d because it is OOM-killed on c5d
  (six validators at 150k, `measurements/vantage/throughput/rep-1/bluestreak/`).

## Cells carried from the a8497560 build

Reported under the `d8e573c9` build; sources preserved in
`measurements/reused-a8497560/`:

- `simpleit-optrbc` @275k (accepted, 95.6%) and @300k (overload) — Simple-IT is
  byte-identical between the two builds (the source delta `175c0ba..fb4a007`
  inside `primary/src/simpleit/` is three cosmetic `..Default::default()` lines;
  its wire format and digests are unchanged).
- `autobahn-seamless` @300k (overload) — the fixed build's 300k deployments
  failed at the progress barrier in all three repetitions, so no measured
  overload point exists on it.

Every reused cell is also flagged in `figures/provenance.json`.

## Integrity

`SHA256SUMS` covers every file in this record.

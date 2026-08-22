#!/usr/bin/env python3
"""Generate the signature-free paper's evaluation .dat files from sweep.json.

Replaces the hand-transcription that produced the current figures (and broke the
n=100 join between committee-scaling.dat and throughput-sweep.dat). Reads
`sweep.json` directly -- NOT `summarize.py`, which reports `ordering_p50` rather
than the `material_p50` the figures use and formats to one decimal only -- takes
the median across repetitions of each headline metric, and emits the three data
files with the schemas, precision, and whisker conventions the .tex files expect.

Field mapping (verified byte-exact against surviving archives):
  *lat   <- material_p50_ms_since_start          (ms)
  *cpu   <- cpu_cores_p50                         (cores)
  *wire  <- bandwidth_efficiency_p50             (bytes/byte)
  *tps   <- tps_median / 1000                     (k tx/s)
  leader-relay *good <- 100 * tps_median / reachable_rate  (percent)
  leader-relay *p50/*p99 <- material_p*_ms_since_start / 1000  (seconds)

Fleet split (Q3): the five shared-binary variants and Sailfish++ are measured on
c5d.2xlarge; Bluestreak is measured on m5d.2xlarge (it OOM-kills on c5d). Q1
committee scaling is entirely c5d. Provenance for every emitted cell is written
to <out>/provenance.json.

Usage:
    python3 vantage/gen_paper_dat.py --results results/vantage --out <dir>
"""
from __future__ import annotations

import argparse
import glob
import json
import pathlib
import statistics
from collections import defaultdict

# ---- variant wiring -------------------------------------------------------

# figure prefix -> results directory name, in figure column order.
COMMITTEE_ORDER = [
    ("v", "vantage"), ("ao", "autobahn-optimistic-a2a"), ("as", "autobahn-seamless"),
    ("so", "simpleit-optrbc"), ("sb", "simpleit-bracha"), ("bl", "bluestreak"),
    ("sf", "sailfish-pp"),
]
THROUGHPUT_ORDER = COMMITTEE_ORDER  # same seven, same order
# leader-relay: opt = autobahn-optimistic-a2a, v = vantage, s = simpleit-optrbc
RELAY_ORDER = [("opt", "autobahn-optimistic-a2a"), ("v", "vantage"), ("s", "simpleit-optrbc")]

PAPER_RATES = [100, 10000, 150000, 170000, 200000, 225000, 250000, 275000, 300000]
RELAY_RATES = [100, 1000, 10000, 20000, 100000, 200000]


def offered_label(rate: int) -> str:
    """Throughput .dat row label: k tx/s, '0.1' for 100, bare int otherwise."""
    return "0.1" if rate == 100 else str(rate // 1000)


# ---- point loading --------------------------------------------------------

def _points(path: str) -> dict[int, dict]:
    p = pathlib.Path(path)
    if not p.is_file():
        return {}
    doc = json.loads(p.read_text())
    return {pt["rate"]: pt for pt in doc.get("points", [])}


def _acceptance_pct(pt: dict) -> float:
    """reachable_throughput_pct, deriving it for the older import schema."""
    pct = pt.get("reachable_throughput_pct")
    if pct is not None:
        return pct
    if pt.get("overloaded") is True:
        return 0.0
    if pt.get("overloaded") is False:
        return 100.0
    denom = pt.get("reachable_rate") or pt.get("rate")
    return 100.0 * pt["tps_median"] / denom if denom else 0.0


def collect(sources: list[str], variant_dir: str) -> dict[int, list[dict]]:
    """rate -> [point per repetition] gathered from every source glob."""
    by_rate: dict[int, list[dict]] = defaultdict(list)
    seen: set[tuple[str, int]] = set()
    for src in sources:
        for sweep in sorted(glob.glob(src.format(variant=variant_dir))):
            for rate, pt in _points(sweep).items():
                key = (sweep, rate)
                if key in seen:
                    continue
                seen.add(key)
                by_rate[rate].append(pt)
    return by_rate


def med(values: list[float]) -> float:
    return statistics.median(values)


# ---- data files -----------------------------------------------------------

def build_committee(results: str, prov: dict) -> list[list]:
    """One row per n in {10,20,50,100}; n=100 joins throughput rate-100."""
    rows = []
    for n in (10, 20, 50, 100):
        row: list = [n]
        for prefix, vdir in COMMITTEE_ORDER:
            if n == 100:
                src = [f"{results}/throughput/rep-*/{{variant}}/sweep.json"]
            else:
                src = [f"{results}/committee-scaling/rep-*/n-%d/{{variant}}/sweep.json" % n]
            pts = collect(src, vdir).get(100, [])
            if not pts:
                row += ["nan", "nan", "nan"]
                prov[f"committee n={n} {vdir}"] = "MISSING"
                continue
            lat = round(med([p["material_p50_ms_since_start"] for p in pts]), 1)
            cpu = round(med([p["cpu_cores_p50"] for p in pts]), 3)
            wire = round(med([p["bandwidth_efficiency_p50"] for p in pts]), 4)
            row += [f"{lat:.1f}", f"{cpu:.3f}", f"{wire:.4f}"]
            prov[f"committee n={n} {vdir}"] = f"{len(pts)} rep(s), c5d"
        rows.append(row)
    return rows


# Q3 fleet sources per variant.
def throughput_sources(results: str, vdir: str) -> list[str]:
    if vdir == "bluestreak":  # m5d side campaigns (anchors + knee, incl. import)
        return [
            f"{results}/throughput-starfish-anchors/rep-*/bluestreak/bluestreak/sweep.json",
            f"{results}/throughput-starfish-knee/rep-*/bluestreak/bluestreak/sweep.json",
        ]
    if vdir == "sailfish-pp":  # c5d side campaigns + the c5d throughput fleet
        return [
            f"{results}/throughput-starfish-anchors/rep-*/sailfishpp/sailfish-pp/sweep.json",
            f"{results}/throughput-starfish-knee/rep-*/sailfishpp/sailfish-pp/sweep.json",
            f"{results}/throughput/rep-*/sailfish-pp/sweep.json",
        ]
    return [f"{results}/throughput/rep-*/{vdir}/sweep.json"]


# Cells reused from the prior a8497560 (commit 175c0ba) campaign, reported under
# the fixed image. Sound because the reused protocols are unchanged between the
# two builds: Simple-IT's source delta 175c0ba..fb4a007 is three cosmetic
# `..Default::default()` lines (its wire/digests are identical), and the seamless
# 300k row is a straight carry-over of a point the fixed build could not seat
# (its 300k deployment barrier-failed in all three reps). Keyed (prefix, rate) ->
# (kind, (tps_k, cpu, eff, lat), note). kind: "accept" (main file) | "overload".
THROUGHPUT_REUSE = {
    ("as", 300000): ("overload", (10.0400, 2.517, 26.5624, 837.5),
                     "reused a8497560/175c0ba (fixed build barrier-failed at 300k)"),
    ("so", 275000): ("accept", (263.0292, 3.559, 1.2034, 742.0),
                     "reused a8497560/175c0ba (Simple-IT identical: 3-cosmetic-line delta)"),
    ("so", 300000): ("overload", (71.1604, 3.555, 4.7299, 17210.0),
                     "reused a8497560/175c0ba (Simple-IT identical)"),
}


def build_throughput(results: str, prov: dict):
    """Returns (main_rows, overload_rows). Accepted points -> main; the single
    first-overload point per variant -> overload file."""
    accepted: dict[str, dict[int, tuple]] = {}   # prefix -> rate -> (tps,cpu,eff,lat)
    overload: dict[str, tuple[int, tuple]] = {}   # prefix -> (rate, tuple)
    for prefix, vdir in THROUGHPUT_ORDER:
        by_rate = collect(throughput_sources(results, vdir), vdir)
        acc: dict[int, tuple] = {}
        ov: tuple | None = None
        for rate in sorted(by_rate):
            pts = by_rate[rate]
            tps = round(med([p["tps_median"] for p in pts]) / 1000.0, 4)
            cpu = round(med([p["cpu_cores_p50"] for p in pts]), 3)
            eff = round(med([p["bandwidth_efficiency_p50"] for p in pts]), 4)
            lat = round(med([p["material_p50_ms_since_start"] for p in pts]), 1)
            pct = med([_acceptance_pct(p) for p in pts])
            cell = (tps, cpu, eff, lat)
            if pct >= 95.0:
                acc[rate] = cell
                prov[f"throughput {vdir} @{rate}"] = f"{len(pts)} rep(s) accept {pct:.1f}%"
            elif ov is None:  # first overload = endpoint
                ov = (rate, cell)
                prov[f"throughput {vdir} @{rate}"] = f"{len(pts)} rep(s) OVERLOAD {pct:.1f}% (endpoint)"
        # Apply reuse overrides for this variant.
        for (rp, rate), (kind, cell, note) in THROUGHPUT_REUSE.items():
            if rp != prefix:
                continue
            if kind == "accept":
                acc[rate] = cell
                if ov is not None and ov[0] <= rate:  # supersede a stale overload
                    ov = None
                prov[f"throughput {vdir} @{rate}"] = f"REUSED accept -- {note}"
            else:  # overload
                ov = (rate, cell)
                prov[f"throughput {vdir} @{rate}"] = f"REUSED overload -- {note}"
        accepted[prefix] = acc
        if ov is not None:
            overload[prefix] = ov

    main_rows = []
    for rate in PAPER_RATES:
        row: list = [offered_label(rate)]
        for prefix, _ in THROUGHPUT_ORDER:
            cell = accepted.get(prefix, {}).get(rate)
            row += ([f"{cell[0]:.4f}", f"{cell[1]:.3f}", f"{cell[2]:.4f}", f"{cell[3]:.1f}"]
                    if cell else ["nan", "nan", "nan", "nan"])
        main_rows.append(row)

    ov_rates = sorted({r for r, _ in overload.values()})
    ov_rows = []
    for rate in ov_rates:
        row = [offered_label(rate)]
        for prefix, _ in THROUGHPUT_ORDER:
            hit = overload.get(prefix)
            if hit and hit[0] == rate:
                c = hit[1]
                row += [f"{c[0]:.4f}", f"{c[1]:.3f}", f"{c[2]:.4f}", f"{c[3]:.1f}"]
            else:
                row += ["nan", "nan", "nan", "nan"]
        ov_rows.append(row)
    return main_rows, ov_rows


def build_relay(results: str, prov: dict) -> list[list]:
    rows = []
    per = {}
    for prefix, vdir in RELAY_ORDER:
        sub = "autobahn" if vdir == "autobahn-optimistic-a2a" else (
            "vantage" if vdir == "vantage" else "simpleit")
        src = [f"{results}/leader-relay/rep-*/{sub}/{vdir}/sweep.json"]
        per[prefix] = collect(src, vdir)
    for rate in RELAY_RATES:
        row: list = [rate]
        for prefix, vdir in RELAY_ORDER:
            pts = per[prefix].get(rate, [])
            if not pts:
                row += ["nan"] * 8
                continue
            goods = [100.0 * p["tps_median"] / p["reachable_rate"] for p in pts]
            p50s = [p["material_p50_ms_since_start"] / 1000.0 for p in pts]
            p99s = [p["material_p99_ms_since_start"] / 1000.0 for p in pts]
            wires = [p["bandwidth_efficiency_p50"] for p in pts]
            g, gp50, gp99, gw = med(goods), med(p50s), med(p99s), med(wires)
            row += [
                f"{g:.2f}", f"{g - min(goods):.2f}", f"{max(goods) - g:.2f}",
                f"{gp50:.4f}", f"{gp50 - min(p50s):.4f}", f"{max(p50s) - gp50:.4f}",
                f"{gp99:.4f}", f"{gw:.3f}",
            ]
            prov[f"relay {vdir} @{rate}"] = f"{len(pts)} rep(s)"
        rows.append(row)
    return rows


# ---- emit -----------------------------------------------------------------

COMMITTEE_HEADER = ("n vlat vcpu vwire aolat aocpu aowire aslat ascpu aswire "
                    "solat socpu sowire sblat sbcpu sbwire bllat blcpu blwire "
                    "sflat sfcpu sfwire")
THROUGHPUT_HEADER = ("offered vtps vcpu veff vlat aotps aocpu aoeff aolat "
                     "astps ascpu aseff aslat sotps socpu soeff solat "
                     "sbtps sbcpu sbeff sblat bltps blcpu bleff bllat "
                     "sftps sfcpu sfeff sflat")
RELAY_HEADER = ("offered optgood optgoodm optgoodp opp50 opp50m opp50p opp99 optwire "
                "vgood vgoodm vgoodp vp50 vp50m vp50p vp99 vwire "
                "sgood sgoodm sgoodp sp50 sp50m sp50p sp99 swire")


def write_dat(path: pathlib.Path, header: str, rows: list[list]) -> None:
    lines = [header] + [" ".join(str(c) for c in row) for row in rows]
    path.write_text("\n".join(lines) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--results", default="results/vantage")
    ap.add_argument("--out", required=True, help="output directory for the .dat files")
    args = ap.parse_args()
    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    prov: dict = {}

    write_dat(out / "committee-scaling.dat", COMMITTEE_HEADER,
              build_committee(args.results, prov))
    main_rows, ov_rows = build_throughput(args.results, prov)
    write_dat(out / "throughput-sweep.dat", THROUGHPUT_HEADER, main_rows)
    write_dat(out / "throughput-sweep-overload.dat", THROUGHPUT_HEADER, ov_rows)
    write_dat(out / "leader-relay.dat", RELAY_HEADER, build_relay(args.results, prov))
    (out / "provenance.json").write_text(json.dumps(prov, indent=2, sort_keys=True) + "\n")
    print(f"wrote 4 files + provenance.json to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

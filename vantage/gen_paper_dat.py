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
  *p99   <- material_p99_ms_since_start          (ms)
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


#: A point that commits less than this fraction of its offered load has not
#: measured a latency: the reporters are cumulative from process start, so the
#: reported percentile still describes the pre-collapse traffic while the
#: submitted backlog never enters the histogram at all. The tell is a latency
#: that *falls* as offered load rises. Such a point keeps its throughput but
#: reports no latency.
COLLAPSED_THROUGHPUT_PCT = 5.0


# ---- data files -----------------------------------------------------------

def build_committee(results: str, prov: dict) -> list[list]:
    """One row per n in {10,20,50,100}; n=100 joins throughput rate-100."""
    rows = []
    for n in (10, 20, 50, 100):
        row: list = [n]
        for prefix, vdir in COMMITTEE_ORDER:
            if n == 100:
                # The n=100 row joins from the throughput ladder's rate-100
                # point USING THAT FIGURE'S OWN SOURCES per variant (Bluestreak
                # m5d, Sailfish side campaigns), so the two figures agree
                # byte-for-byte on every column.
                src = throughput_sources(results, vdir)
            else:
                src = [f"{results}/committee-scaling/rep-*/n-%d/{{variant}}/sweep.json" % n]
            pts = collect(src, vdir).get(100, [])
            if not pts:
                row += ["nan", "nan", "nan", "nan"]
                prov[f"committee n={n} {vdir}"] = "MISSING"
                continue
            lat = round(med([p["material_p50_ms_since_start"] for p in pts]), 1)
            cpu = round(med([p["cpu_cores_p50"] for p in pts]), 3)
            wire = round(med([p["bandwidth_efficiency_p50"] for p in pts]), 4)
            p99 = round(med([p["material_p99_ms_since_start"] for p in pts]), 1)
            row += [f"{lat:.1f}", f"{cpu:.3f}", f"{wire:.4f}", f"{p99:.1f}"]
            fleet = "join(fig sources)" if n == 100 else "c5d"
            prov[f"committee n={n} {vdir}"] = f"{len(pts)} rep(s), {fleet}"
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
# (kind, (tps_k, cpu, eff, lat, p99), note). kind: "accept" (main) | "overload".
THROUGHPUT_REUSE = {
    ("as", 300000): ("overload", (10.0400, 2.517, 26.5624, None, None),
                     "reused a8497560/175c0ba (fixed build barrier-failed at 300k); "
                     "collapsed at 3.3% of offered, so its cumulative p50 read of "
                     "837.5 ms describes pre-collapse traffic only and is not emitted"),
    ("so", 275000): ("accept", (263.0292, 3.559, 1.2034, 742.0, 992.0),
                     "reused a8497560/175c0ba (Simple-IT identical: 3-cosmetic-line delta)"),
    ("so", 300000): ("overload", (71.1604, 3.555, 4.7299, 17210.0, 54157.5),
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
            p99 = round(med([p["material_p99_ms_since_start"] for p in pts]), 1)
            pct = med([_acceptance_pct(p) for p in pts])
            cell = (tps, cpu, eff, lat, p99)
            if pct >= 95.0:
                acc[rate] = cell
                prov[f"throughput {vdir} @{rate}"] = f"{len(pts)} rep(s) accept {pct:.1f}%"
            elif ov is None:  # first overload = endpoint
                if pct < COLLAPSED_THROUGHPUT_PCT:
                    # Keep throughput/CPU/wire; drop the unmeasurable latency.
                    ov = (rate, (tps, cpu, eff, None, None))
                    prov[f"throughput {vdir} @{rate}"] = (
                        f"{len(pts)} rep(s) COLLAPSED {pct:.1f}% (endpoint; "
                        f"latency not measurable, cumulative p50 read "
                        f"{lat:.1f} ms describes pre-collapse traffic only)")
                else:
                    ov = (rate, cell)
                    prov[f"throughput {vdir} @{rate}"] = (
                        f"{len(pts)} rep(s) OVERLOAD {pct:.1f}% (endpoint)")
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
        hits = 0
        for prefix, _ in THROUGHPUT_ORDER:
            cell = accepted.get(prefix, {}).get(rate)
            if cell:
                hits += 1
                row += [f"{cell[0]:.4f}", f"{cell[1]:.3f}", f"{cell[2]:.4f}",
                        f"{cell[3]:.1f}", f"{cell[4]:.1f}"]
            else:
                row += ["nan"] * 5
        # An all-nan row is not a data point, and under pgfplots'
        # `unbounded coords=jump` it severs every series' polyline at that x.
        if hits:
            main_rows.append(row)

    ov_rates = sorted({r for r, _ in overload.values()})
    ov_rows = []
    for rate in ov_rates:
        row = [offered_label(rate)]
        for prefix, _ in THROUGHPUT_ORDER:
            hit = overload.get(prefix)
            if hit and hit[0] == rate:
                c = hit[1]
                row += [f"{c[0]:.4f}", f"{c[1]:.3f}", f"{c[2]:.4f}",
                        "nan" if c[3] is None else f"{c[3]:.1f}",
                        "nan" if c[4] is None else f"{c[4]:.1f}"]
            else:
                row += ["nan"] * 5
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

COMMITTEE_HEADER = ("n vlat vcpu vwire vp99 aolat aocpu aowire aop99 "
                    "aslat ascpu aswire asp99 solat socpu sowire sop99 "
                    "sblat sbcpu sbwire sbp99 bllat blcpu blwire blp99 "
                    "sflat sfcpu sfwire sfp99")
THROUGHPUT_HEADER = ("offered vtps vcpu veff vlat vp99 aotps aocpu aoeff aolat aop99 "
                     "astps ascpu aseff aslat asp99 sotps socpu soeff solat sop99 "
                     "sbtps sbcpu sbeff sblat sbp99 bltps blcpu bleff bllat blp99 "
                     "sftps sfcpu sfeff sflat sfp99")
RELAY_HEADER = ("offered optgood optgoodm optgoodp opp50 opp50m opp50p opp99 optwire "
                "vgood vgoodm vgoodp vp50 vp50m vp50p vp99 vwire "
                "sgood sgoodm sgoodp sp50 sp50m sp50p sp99 swire")


def write_dat(path: pathlib.Path, header: str, rows: list[list]) -> None:
    lines = [header] + [" ".join(str(c) for c in row) for row in rows]
    path.write_text("\n".join(lines) + "\n")


# ---- generated LaTeX: the p50/p99 companion table -------------------------

#: Column heads for the percentile table, in figure column order.
TABLE_HEADS = ["\\sysname{}", "A2A", "Seamless", "IT-Opt", "IT-Bracha",
               "Bluestreak", "Sailfish++"]
#: Leader-relay variant -> figure column prefix. The stress covers three of
#: the seven variants, so the other four columns are absent in that block.
RELAY_TO_COLUMN = {"v": "v", "op": "ao", "s": "so"}
#: Throughput rows to tabulate: (.dat offered label, printed label).
TABLE_RATES = [("0.1", "100 tx/s"), ("10", "10k"), ("150", "150k"),
               ("200", "200k"), ("225", "225k"), ("250", "250k"), ("275", "275k")]


def _cells(header: str, row: list[str]) -> list[str]:
    """`p50/p99` per variant, in figure column order, from an emitted row."""
    idx = {name: i for i, name in enumerate(header.split())}
    out = []
    for prefix, _ in COMMITTEE_ORDER:
        lat, p99 = row[idx[f"{prefix}lat"]], row[idx[f"{prefix}p99"]]
        out.append("---" if lat == "nan" else
                   f"{float(lat):.0f}/{float(p99):.0f}")
    return out


def _relay_cells(row: list[str]) -> list[str]:
    """`p50/p99` in ms for the three stressed variants, absent elsewhere."""
    idx = {name: i for i, name in enumerate(RELAY_HEADER.split())}
    inv = {col: pre for pre, col in RELAY_TO_COLUMN.items()}
    out = []
    for prefix, _ in COMMITTEE_ORDER:
        pre = inv.get(prefix)
        if pre is None or row[idx[f"{pre}p50"]] == "nan":
            out.append("---")
            continue
        p50 = float(row[idx[f"{pre}p50"]]) * 1000.0
        p99 = float(row[idx[f"{pre}p99"]]) * 1000.0
        out.append(f"{p50:.0f}/{p99:.0f}")
    return out


def write_percentile_table(path: pathlib.Path, committee_rows: list[list],
                           throughput_rows: list[list],
                           relay_rows: list[list]) -> None:
    """Emit the p50/p99 table body so it is generated, never transcribed."""
    lines = [
        "% Generated by vantage/gen_paper_dat.py -- do not edit by hand.",
        "\\begin{tabular}{@{}l" + "r" * len(TABLE_HEADS) + "@{}}",
        "\\toprule",
        " & " + " & ".join(TABLE_HEADS) + " \\\\",
        "\\midrule",
        "\\multicolumn{%d}{@{}l}{\\emph{Committee scaling at 100 tx/s"
        "} (\\Cref{fig:committee-scaling})} \\\\" % (len(TABLE_HEADS) + 1),
    ]
    for row in committee_rows:
        cells = _cells(COMMITTEE_HEADER, [str(c) for c in row])
        lines.append(f"$n{{=}}{row[0]}$ & " + " & ".join(cells) + " \\\\")
    lines += [
        "\\midrule",
        "\\multicolumn{%d}{@{}l}{\\emph{Throughput ladder at $n=100$, accepted "
        "rungs} (\\Cref{fig:throughput-sweep})} \\\\" % (len(TABLE_HEADS) + 1),
    ]
    by_label = {str(r[0]): [str(c) for c in r] for r in throughput_rows}
    for key, label in TABLE_RATES:
        row = by_label.get(key)
        if row is None:
            continue
        lines.append(f"{label} & " + " & ".join(_cells(THROUGHPUT_HEADER, row))
                     + " \\\\")
    lines += [
        "\\midrule",
        "\\multicolumn{%d}{@{}l}{\\emph{Leader-relay stress at $n=20$, $f=6$"
        "} (\\Cref{fig:leader-relay})} \\\\" % (len(TABLE_HEADS) + 1),
    ]
    for row in relay_rows:
        row = [str(c) for c in row]
        rate = int(row[0])
        label = f"{rate // 1000}k" if rate >= 1000 else f"{rate} tx/s"
        lines.append(f"{label} & " + " & ".join(_relay_cells(row)) + " \\\\")
    lines += ["\\bottomrule", "\\end{tabular}"]
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
    relay_rows = build_relay(args.results, prov)
    write_dat(out / "leader-relay.dat", RELAY_HEADER, relay_rows)
    write_percentile_table(out / "latency-percentiles.tex",
                           build_committee(args.results, {}), main_rows, relay_rows)
    (out / "provenance.json").write_text(json.dumps(prov, indent=2, sort_keys=True) + "\n")
    print(f"wrote 4 .dat + latency-percentiles.tex + provenance.json to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

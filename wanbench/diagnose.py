"""Compare healthy and degraded validators in Vantage scrape files."""

from __future__ import annotations

import argparse
import collections
import glob
import os
import re
import statistics

# Metrics are ordered from output progress toward transport load.
CHAIN: list[tuple[str, str]] = [
    ("committed_transactions", "output: transactions committed"),
    ("vantage_cursor_next_view", "output: cursor position"),
    ("vantage_seals", "consensus: views sealed"),
    ("vantage_entered_view", "consensus: views entered"),
    ("vantage_entry_target", "pacemaker: target (== omega_q)"),
    ("vantage_omega_q", "pacemaker: 2f+1-th observed wish"),
    ("vantage_own_watermark", "pacemaker: own wish (target's headroom)"),
    ("vantage_blocks_published", "lane: own blocks published"),
    ("vantage_blocks_received", "lane: blocks received"),
    ("vantage_repairs_requested", "repair: requests sent   <-- x99 if regressed"),
    ("vantage_repair_fanout_pending", "repair: gap size (outstanding digests)"),
    ("vantage_repair_fanout_escalations_total", "repair: rounds beyond the first"),
    ("vantage_pending_settle_len", "repair: authorized-but-unsettled"),
    ("vantage_pending_body_fetch_len", "agb: pending body fetches"),
    ("vantage_pending_gate_len", "agb: gated views"),
    ("vantage_bulk_inbound_dropped_total", "queue: bulk drops  <-- the flood signal"),
    ("network_volatile_shed_total", "queue: volatile shed"),
    ("network_messages_received_total", "wire: messages received"),
    ("vantage_control_round", "control: round"),
]


def _load(path: str) -> dict[str, float]:
    """Sum every sample of each family (labelled series collapse into one total)."""
    out: dict[str, float] = {}
    with open(path) as fh:
        for line in fh:
            if not line.strip() or line.startswith("#"):
                continue
            name = line.split("{")[0].split(" ")[0]
            try:
                out[name] = out.get(name, 0.0) + float(line.rsplit(None, 1)[1])
            except (ValueError, IndexError):
                continue
    return out


def _seal_routes(path: str) -> collections.Counter:
    routes: collections.Counter = collections.Counter()
    with open(path) as fh:
        for line in fh:
            if line.startswith("vantage_seals{"):
                m = re.search(r'route="([^"]+)"', line)
                if m:
                    try:
                        routes[m.group(1)] += float(line.rsplit(None, 1)[1])
                    except (ValueError, IndexError):
                        pass
    return routes


def load_dir(
    d: str, prefix: str | None = None
) -> tuple[dict[int, dict[str, float]], dict[int, collections.Counter], str]:
    """Load exactly one scrape snapshot from a point directory."""
    prefixes = sorted({
        m.group(1)
        for f in glob.glob(os.path.join(d, "*node-*.prom"))
        if (m := re.match(r"(.*?)node-\d+\.prom$", os.path.basename(f)))
    })
    if prefix is None:
        # Prefer the latest available snapshot.
        for p in ("final-", "quality-", "barrier-", "baseline-"):
            if p in prefixes:
                prefix = p
                break
        else:
            prefix = prefixes[0] if prefixes else ""
    rows: dict[int, dict[str, float]] = {}
    routes: dict[int, collections.Counter] = {}
    for f in sorted(glob.glob(os.path.join(d, f"{prefix}node-*.prom"))):
        m = re.search(r"node-(\d+)", os.path.basename(f))
        if not m:
            continue
        i = int(m.group(1))
        rows[i] = _load(f)
        routes[i] = _seal_routes(f)
    return rows, routes, prefix


def _med(rows: dict[int, dict[str, float]], group, key: str) -> float:
    xs = [rows[i].get(key, 0.0) for i in group]
    return statistics.median(xs) if xs else 0.0


def report(d: str, cursor_floor: int | None = None, prefix: str | None = None) -> int:
    rows, routes, used = load_dir(d, prefix)
    if not rows:
        print(f"no scrapes found in {d}")
        return 1
    n = len(rows)
    peers = n - 1

    cursors = {i: rows[i].get("vantage_cursor_next_view", 0.0) for i in rows}
    if cursor_floor is None:
        # Use a fixed fraction so uniformly slow runs remain one cohort.
        cursor_floor = int(0.8 * max(cursors.values())) if cursors else 0
    healthy = sorted(i for i in rows if cursors[i] >= cursor_floor)
    degraded = sorted(i for i in rows if cursors[i] < cursor_floor)

    print(f"=== {d}  [snapshot: {used or '(none)'}]")
    print(f"nodes {n}  healthy {len(healthy)}  degraded {len(degraded)}  "
          f"(cursor floor {cursor_floor}, best {int(max(cursors.values()))})")
    if degraded:
        print(f"degraded indices: {degraded}")
    print()
    print(f"{'metric':44} {'healthy':>13} {'degraded':>13} {'ratio':>8}")
    print("-" * 82)
    for key, label in CHAIN:
        h = _med(rows, healthy, key)
        g = _med(rows, degraded, key) if degraded else 0.0
        ratio = "-" if h == 0 else f"{g / h:.3f}"
        flag = ""
        if key == "vantage_repairs_requested" and g and peers and g % peers == 0:
            flag = f"  << EXACT multiple of {peers}: full fan-out"
        print(f"{label:44} {h:13,.0f} {g:13,.0f} {ratio:>8}{flag}")

    all_routes = sorted({r for i in routes for r in routes[i]})
    if all_routes:
        print()
        tot_h = sum(statistics.median([routes[i].get(r, 0) for i in healthy])
                    for r in all_routes) or 1
        print(f"{'seal route':44} {'healthy':>13} {'degraded':>13} {'h share':>8}")
        print("-" * 82)
        for r in all_routes:
            h = statistics.median([routes[i].get(r, 0) for i in healthy])
            g = (statistics.median([routes[i].get(r, 0) for i in degraded])
                 if degraded else 0.0)
            print(f"{r:44} {h:13,.0f} {g:13,.0f} {h / tot_h * 100:7.1f}%")
        if degraded:
            # Missing proposers increase vote_skip in proportion to degraded nodes.
            skip = statistics.median([routes[i].get("vote_skip", 0) for i in healthy])
            print(f"\nvote_skip share of healthy seals {skip / tot_h * 100:.1f}% vs "
                  f"degraded fraction {len(degraded) / n * 100:.1f}% -- these track when "
                  f"lagging nodes' proposer slots go unfilled")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="wanbench.diagnose")
    p.add_argument("scrape_dir")
    p.add_argument("--prefix", default=None,
                   help="scrape snapshot to read (final-, quality-, barrier-, baseline-); counters from different snapshots are NOT comparable")
    p.add_argument("--cursor-floor", type=int, default=None,
                   help="cursor below which a node counts as degraded "
                        "(default: 80%% of the best node's cursor)")
    a = p.parse_args(argv)
    return report(a.scrape_dir, a.cursor_floor, a.prefix)


if __name__ == "__main__":
    raise SystemExit(main())

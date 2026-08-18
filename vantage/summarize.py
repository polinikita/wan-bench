"""Median-of-repetitions summary for one paper question.

Usage: python3 vantage/summarize.py <out_base>
Walks <out_base>/rep-*/<variant>/sweep.json (and n-*/ subdirs for the
committee-scaling layout), groups points by (variant, committee size, rate),
and reports the median across repetitions of every headline metric, with the
per-repetition spread so instability is visible rather than averaged away.
The median row is what goes into the paper's .dat figures.
"""
import json
import pathlib
import statistics
import sys

METRICS = [
    ("tps_median", "committed tx/s"),
    ("ordering_p50_ms_since_start", "p50 ms"),
    ("cpu_cores_p50", "cpu cores"),
    ("wire_bytes_per_tx_p50", "wire B/tx"),
    ("bandwidth_efficiency_p50", "bw eff"),
]


def sweeps(base: pathlib.Path):
    for sweep in sorted(base.glob("rep-*/**/sweep.json")):
        rep = sweep.relative_to(base).parts[0]
        # Layouts: rep-N/<variant>/sweep.json or rep-N/n-<size>/<variant>/sweep.json.
        middle = sweep.relative_to(base / rep).parts[:-1]
        size = None
        variant_parts = []
        for part in middle:
            if part.startswith("n-"):
                size = int(part[2:])
            else:
                variant_parts.append(part)
        variant = "/".join(variant_parts)
        yield rep, variant, size, json.loads(sweep.read_text())


def main() -> int:
    base = pathlib.Path(sys.argv[1])
    cells: dict = {}
    reps_seen: set = set()
    for rep, variant, size, sweep in sweeps(base):
        reps_seen.add(rep)
        for p in sweep.get("points", []):
            key = (variant, size if size is not None else sweep.get("n"), p.get("rate"))
            cells.setdefault(key, {}).setdefault(rep, p)
    if not cells:
        print(f"no sweep.json under {base}/rep-*/")
        return 1
    print(f"repetitions found: {sorted(reps_seen)}\n")
    header = f"{'variant':<26}{'n':>5}{'rate':>9}"
    for _, label in METRICS:
        header += f"{label:>16}{'spread':>20}"
    print(header)
    for (variant, size, rate), by_rep in sorted(cells.items()):
        row = f"{variant:<26}{size or '':>5}{rate:>9,}"
        for field, _ in METRICS:
            values = [p.get(field) for p in by_rep.values() if p.get(field) is not None]
            if not values:
                row += f"{'-':>16}{'-':>20}"
                continue
            med = statistics.median(values)
            row += f"{med:>16,.1f}{f'[{min(values):,.1f}..{max(values):,.1f}]':>20}"
        marker = "" if len(by_rep) == len(reps_seen) else f"   (only {sorted(by_rep)})"
        print(row + marker)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

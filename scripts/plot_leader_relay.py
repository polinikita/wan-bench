#!/usr/bin/env python3
"""Plot the isolated leader-relay capacity sweeps from sweep.json files."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
from matplotlib.ticker import FuncFormatter


SERIES = (
    ("Autobahn Optimistic", "#D55E00", "o"),
    ("Vantage", "#0072B2", "s"),
    ("Simple-IT Opt-RBC", "#009E73", "^"),
)


def load_points(path: str) -> list[dict]:
    data = json.loads(Path(path).read_text())
    points = data.get("points")
    if not isinstance(points, list) or not points:
        raise ValueError(f"{path}: sweep has no measured points")
    return points


def rate_label(value: float, _position: int) -> str:
    if value >= 1_000:
        return f"{value / 1_000:g}k"
    return f"{value:g}"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--optimistic", required=True)
    parser.add_argument("--vantage", required=True)
    parser.add_argument("--simple-it", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    datasets = (
        load_points(args.optimistic),
        load_points(args.vantage),
        load_points(args.simple_it),
    )
    rates = [point["rate"] for point in datasets[0]]
    for points in datasets[1:]:
        if [point["rate"] for point in points] != rates:
            raise ValueError("all sweeps must use the same offered-rate ladder")

    plt.rcParams.update({
        "font.size": 9,
        "axes.labelsize": 10,
        "axes.titlesize": 10,
        "legend.fontsize": 8,
        "xtick.labelsize": 8,
        "ytick.labelsize": 8,
        "axes.spines.top": False,
        "axes.spines.right": False,
    })
    fig, axes = plt.subplots(1, 3, figsize=(12.2, 3.55), constrained_layout=True)

    for (label, color, marker), points in zip(SERIES, datasets):
        x = [point["rate"] for point in points]
        useful = [100 * point["tps_median"] / point["reachable_rate"]
                  for point in points]
        p50 = [point["material_p50_ms_since_start"] / 1_000 for point in points]
        p99 = [point["material_p99_ms_since_start"] / 1_000 for point in points]
        wire = [point["wire_mb_per_s_p50"] for point in points]

        axes[0].plot(x, useful, color=color, marker=marker, linewidth=1.8,
                     markersize=5, label=label)
        axes[1].plot(x, p50, color=color, marker=marker, linewidth=1.8,
                     markersize=5)
        axes[1].plot(x, p99, color=color, linestyle="--", linewidth=1.1,
                     alpha=0.72)
        axes[2].plot(x, wire, color=color, marker=marker, linewidth=1.8,
                     markersize=5)

    optimistic_points = datasets[0]
    byzantine = [point["uncounted_throughput_pct"] for point in optimistic_points]
    axes[0].plot(rates, byzantine, color="#D55E00", marker="D",
                 linestyle=":", linewidth=1.5, markersize=4,
                 label="Optimistic: Byzantine share")

    degraded = optimistic_points[-1]
    if degraded.get("healthy_nodes_final", degraded["nodes"]) < degraded["nodes"]:
        x = degraded["rate"]
        useful = 100 * degraded["tps_median"] / degraded["reachable_rate"]
        axes[0].scatter([x], [useful], marker="x", color="#7A1C00", s=65,
                        linewidths=2.0, zorder=6)
        axes[0].annotate(
            f"degraded: {degraded['healthy_nodes_final']}/{degraded['nodes']} healthy",
            xy=(x, useful), xytext=(-88, -28), textcoords="offset points",
            arrowprops={"arrowstyle": "->", "color": "#555555", "lw": 0.8},
            fontsize=8,
        )

    axes[0].axhline(100, color="#777777", linestyle="--", linewidth=0.8)
    axes[0].set_title("(a) Sustainable goodput")
    axes[0].set_ylabel("Delivered share (%)")
    axes[0].set_ylim(45, 105)
    axes[0].legend(loc="lower left", frameon=False)

    axes[1].set_title("(b) Materialized latency")
    axes[1].set_ylabel("Latency (s)")
    axes[1].set_ylim(bottom=0)
    latency_legend = (
        Line2D([0], [0], color="#444444", lw=1.8, label="p50"),
        Line2D([0], [0], color="#444444", lw=1.1, ls="--", label="p99"),
    )
    axes[1].legend(handles=latency_legend, loc="upper left", frameon=False)

    axes[2].set_title("(c) Median validator egress")
    axes[2].set_ylabel("Wire traffic (MB/s)")
    axes[2].set_yscale("log")
    axes[2].set_ylim(0.1, 180)

    for axis in axes:
        axis.set_xscale("log")
        axis.set_xticks(rates)
        axis.xaxis.set_major_formatter(FuncFormatter(rate_label))
        axis.tick_params(axis="x", labelrotation=25)
        axis.minorticks_off()
        axis.grid(axis="y", color="#D9D9D9", linewidth=0.7)
        axis.set_xlabel("Total offered load (tx/s)")

    fig.suptitle(
        "Leader-relay stress, n=20 and f=6 Byzantine publishers "
        "(AWS private-IP netem, c5d.2xlarge)",
        fontsize=11,
    )
    output = Path(args.out)
    output.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output, dpi=240, bbox_inches="tight")


if __name__ == "__main__":
    main()

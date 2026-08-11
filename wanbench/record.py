"""Create a self-contained benchmark record from a sweep."""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import pathlib
import shutil

import yaml

# Per-point table columns: JSON key, heading, and format.
_COLUMNS: list[tuple[str, str, str]] = [
    ("rate", "offered tx/s", ",d"),
    ("tps_median", "committed tx/s (median)", ",.0f"),
    ("tps_min", "committed tx/s (min)", ",.0f"),
    # Materialised latency is the cross-protocol value.
    ("ordering_p50_ms_since_start", "ord p50 ms", ".0f"),
    ("ordering_p95_ms_since_start", "ord p95 ms", ".0f"),
    ("ordering_p99_ms_since_start", "ord p99 ms", ".0f"),
    ("material_p50_ms_since_start", "mat p50 ms", ".0f"),
    ("material_p99_ms_since_start", "mat p99 ms", ".0f"),
    ("cpu_cores_p50", "CPU cores p50/node", ".2f"),
    # Per-validator bandwidth remains comparable as committee size changes.
    ("wire_mb_per_s_p50", "wire MB/s p50/node", ".2f"),
    ("bandwidth_efficiency_p50", "wire B / sequenced B", ".2f"),
    ("estimated_non_payload_bytes_per_tx_p50", "non-payload B/tx", ".0f"),
]

# Inline the fields needed to attribute a result.
_PROVENANCE = ("image", "instance_type", "region", "protocol", "nodes",
               "tx_size", "delta_ms", "spot")


def _fmt(value, spec: str) -> str:
    if value is None:
        return "--"
    try:
        return format(value, spec)
    except (TypeError, ValueError):
        return str(value)


def _provenance_lines(config_text: str) -> list[str]:
    """Extract top-level provenance fields from config YAML."""
    found: dict[str, str] = {}
    for raw in config_text.splitlines():
        if not raw or raw[0].isspace() or raw.lstrip().startswith("#"):
            continue
        key, sep, rest = raw.partition(":")
        if not sep:
            continue
        key = key.strip()
        if key in _PROVENANCE and key not in found:
            found[key] = rest.split("#")[0].strip() or "(unset)"
    return [f"- `{k}`: {found[k]}" for k in _PROVENANCE if k in found]


def distill(data: dict, config_text: str) -> str:
    """Render provenance, measurements, and outcome as Markdown."""
    nodes = data.get("nodes")
    protocol = data.get("protocol", "?")
    out: list[str] = [
        f"# {protocol} n={nodes} load sweep",
        "",
        "## Provenance",
        "",
    ]
    out += _provenance_lines(config_text) or ["- (config had no recognized fields)"]
    out += [
        f"- warmup / window: {data.get('warmup_s')} s / {data.get('window_s')} s per point",
        f"- rate ladder: {', '.join(f'{r:,}' for r in data.get('rates', []))}",
        f"- early-stop threshold: {data.get('drop_tolerance_pct')}% committed-TPS drop",
        "",
        "## Points",
        "",
    ]

    points = data.get("points", [])
    if not points:
        out += ["No points recorded.", ""]
    else:
        out.append("| " + " | ".join(h for _, h, _ in _COLUMNS) + " | healthy |")
        out.append("|" + "|".join("---" for _ in range(len(_COLUMNS) + 1)) + "|")
        for p in points:
            cells = [_fmt(p.get(k), spec) for k, _, spec in _COLUMNS]
            base, final = p.get("healthy_nodes_baseline"), p.get("healthy_nodes_final")
            health = f"{base}/{nodes}" if base == final else f"{base}->{final} of {nodes}"
            out.append("| " + " | ".join(cells) + f" | {health} |")
        out.append("")

    status = data.get("status", "?")
    out += ["## Outcome", "", f"- status: **{status}**"]
    if data.get("stopped_early"):
        out.append(f"- stopped early: {data.get('stop_reason')}")
    else:
        out.append("- ran the full rate ladder without early stop")
    if data.get("error"):
        out.append(f"- error: `{data['error']}` (treat the final point as suspect)")
    timeline = data.get("timeline") or {}
    if timeline.get("total_s"):
        out.append(f"- wall clock: {round(timeline['total_s'])} s")
    out.append("")
    return "\n".join(out)


def record(sweep_path: str, config_path: str | None = None, dest: str = "recorded",
           stamp: str | None = None) -> pathlib.Path:
    sweep_file = pathlib.Path(sweep_path)
    data = json.loads(sweep_file.read_text())
    effective = data.get("effective_config")
    if effective:
        config_text = yaml.safe_dump(effective, sort_keys=False)
    elif config_path:
        config_text = pathlib.Path(config_path).read_text()
    else:
        raise ValueError("sweep has no effective_config; pass --config for an older run")

    run_id = data.get("points", [{}])[0].get("run_id") if data.get("points") else None
    run_id = run_id or sweep_file.parent.name
    name = f"{run_id}-{stamp}" if stamp else run_id
    out = pathlib.Path(dest) / name
    out.mkdir(parents=True, exist_ok=True)

    shutil.copy2(sweep_file, out / "sweep.json")
    (out / "config.yaml").write_text(config_text)
    (out / "README.md").write_text(distill(data, config_text))
    return out


def _sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def record_matrix(matrix_path: str, config_path: str | None = None,
                  dest: str = "recorded",
                  stamp: str | None = None) -> pathlib.Path:
    """Promote a completed committee matrix to a compact paper record."""
    matrix_file = pathlib.Path(matrix_path)
    source = matrix_file.parent
    matrix = json.loads(matrix_file.read_text())
    if matrix.get("kind") != "committee-matrix":
        raise ValueError(f"not a committee matrix: {matrix_file}")
    if matrix.get("status") != "completed":
        raise ValueError(
            f"matrix is {matrix.get('status', 'unknown')}, not completed")

    name = matrix.get("name") or source.name
    name = f"{name}-{stamp}" if stamp else name
    out = pathlib.Path(dest) / name
    if out.exists() and any(out.iterdir()):
        raise FileExistsError(f"record already exists and is not empty: {out}")
    out.mkdir(parents=True, exist_ok=True)

    committees = []
    raw_archives = []
    for entry in matrix.get("committees", []):
        nodes = entry["nodes"]
        child = source / entry["output"]
        campaign_path = child / "campaign.json"
        campaign = json.loads(campaign_path.read_text())
        variants = []
        for variant in campaign.get("variants", []):
            sweep_path = child / variant["output"] / "sweep.json"
            variants.append({
                "name": variant["name"],
                "sweep": json.loads(sweep_path.read_text()),
            })
        committees.append({
            "nodes": nodes,
            "campaign": campaign,
            "variants": variants,
        })

        archive = child / "prometheus-tsdb.tar.gz"
        if archive.is_file():
            raw_archives.append({
                "nodes": nodes,
                "source": str(archive.relative_to(source)),
                "bytes": archive.stat().st_size,
                "sha256": _sha256(archive),
            })

    measurements = {
        "schema_version": 1,
        "kind": "committee-scaling-record",
        "recorded_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "matrix": matrix,
        "committees": committees,
        "raw_prometheus_archives": raw_archives,
    }
    (out / "measurements.json").write_text(
        json.dumps(measurements, indent=2, allow_nan=False) + "\n")
    for filename in ("points.csv", "points.json", "matrix.json"):
        shutil.copy2(source / filename, out / filename)
    if config_path:
        shutil.copy2(config_path, out / "config.yaml")

    source_readme = source / "README.md"
    lines = [source_readme.read_text().rstrip()] if source_readme.is_file() else [
        f"# {matrix.get('name', 'committee scaling')}"
    ]
    lines += [
        "",
        "## Record",
        "",
        "This directory contains the complete summary measurements and exact "
        "campaign definitions used for the paper sweep.",
        "",
        "- `points.csv`: plot-ready table",
        "- `points.json`: plot-ready structured rows",
        "- `measurements.json`: per-point details, fleet provenance, and image digests",
        "- `matrix.json`: execution status and definitions",
        "- `config.yaml`: source campaign, when supplied",
        "",
        "Raw Prometheus databases are not stored in Git. Their paths, sizes, and "
        "SHA-256 digests are recorded in `measurements.json`.",
        "",
    ]
    (out / "README.md").write_text("\n".join(lines))

    tracked = sorted(path for path in out.iterdir()
                     if path.is_file() and path.name != "SHA256SUMS")
    checksums = [f"{_sha256(path)}  {path.name}" for path in tracked]
    (out / "SHA256SUMS").write_text("\n".join(checksums) + "\n")
    return out


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="wanbench.record")
    source = p.add_mutually_exclusive_group(required=True)
    source.add_argument("--sweep", help="path to a sweep.json")
    source.add_argument("--matrix", help="path to a completed matrix.json")
    p.add_argument("--config", help="source config for older sweep files")
    p.add_argument("--dest", default="recorded", help="tracked output root")
    p.add_argument("--stamp", default=None, help="suffix, e.g. 20260806")
    a = p.parse_args(argv)
    out = (record_matrix(a.matrix, a.config, a.dest, a.stamp)
           if a.matrix else record(a.sweep, a.config, a.dest, a.stamp))
    print(f"recorded -> {out}")
    for f in sorted(out.iterdir()):
        print(f"  {f.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

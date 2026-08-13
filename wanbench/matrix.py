"""Run one campaign across independently provisioned committee sizes."""

from __future__ import annotations

import csv
import datetime
import json
import pathlib

from .aws import Aws
from .campaign import (CampaignConfig, _checkpoint, _definition,
                       _prepare_configs, execute)
from .config import RunConfig

MatrixGroup = tuple[int, CampaignConfig, list[tuple[str, RunConfig]]]


def preflight(campaign: CampaignConfig,
              only: set[str] | None = None) -> tuple[list[MatrixGroup], dict]:
    """Prepare sequential committee campaigns and check the largest fleet."""
    groups = []
    for nodes in campaign.committee_sizes:
        child = campaign.for_committee(nodes)
        groups.append((nodes, child, child.configs(only)))

    all_configs = [item for _nodes, _child, configs in groups for item in configs]
    pinned = _prepare_configs(all_configs)
    largest = groups[-1][2][0][1]
    aws_report = Aws(largest).preflight()
    measured_per_fleet_s = len(groups[0][2]) * len(campaign.rates) * (
        campaign.warmup_s + campaign.window_s)
    fleets = [
        {"nodes": nodes, "instances": nodes + 1}
        for nodes in campaign.committee_sizes
    ]
    report = {
        **aws_report,
        "name": campaign.name,
        "output": campaign.output,
        "nodes": campaign.committee_sizes[-1],
        "instances": campaign.committee_sizes[-1] + 1,
        "instance_type": largest.instance_type or "auto",
        "az": largest.az or "auto-select one AZ",
        "sweep_field": campaign.sweep_field,
        "rates": campaign.rates,
        "warmup_s": campaign.warmup_s,
        "window_s": campaign.window_s,
        "stop_on_drop": campaign.stop_on_drop,
        "strict_through_rate": campaign.strict_through_rate,
        "min_offered_throughput_pct": campaign.min_offered_throughput_pct,
        "variants": [name for name, _cfg in groups[0][2]],
        "images": pinned,
        "committee_sizes": campaign.committee_sizes,
        "sequential_fleets": fleets,
        "minimum_duration_s": len(groups) * measured_per_fleet_s,
        "minimum_instance_hours": sum(
            fleet["instances"] * measured_per_fleet_s / 3600
            for fleet in fleets
        ),
    }
    return groups, report


def _definition_matrix(campaign: CampaignConfig,
                       groups: list[MatrixGroup]) -> dict:
    return {
        "name": campaign.name,
        "committee_sizes": campaign.committee_sizes,
        "committees": [
            {"nodes": nodes, "definition": _definition(child, configs)}
            for nodes, child, configs in groups
        ],
    }


def _new_state(campaign: CampaignConfig, groups: list[MatrixGroup]) -> dict:
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    return {
        "schema_version": 1,
        "kind": "committee-matrix",
        "name": campaign.name,
        "status": "running",
        "error": None,
        "started_at": now,
        "finished_at": None,
        "definition": _definition_matrix(campaign, groups),
        "committees": [
            {
                "nodes": nodes,
                "status": "pending",
                "error": None,
                "output": f"n-{nodes}",
            }
            for nodes, _child, _configs in groups
        ],
    }


def _load_state(path: pathlib.Path, campaign: CampaignConfig,
                groups: list[MatrixGroup], resume: bool) -> dict:
    if not path.exists():
        if resume:
            raise RuntimeError(f"matrix state does not exist: {path}")
        return _new_state(campaign, groups)
    if not resume:
        raise RuntimeError(f"campaign output already exists: {path}; use --resume")
    state = json.loads(path.read_text())
    if state.get("schema_version") != 1 or state.get("kind") != "committee-matrix":
        raise RuntimeError("unsupported committee matrix state")
    if state.get("definition") != _definition_matrix(campaign, groups):
        raise RuntimeError(
            "campaign definition changed; resume with the original settings and "
            "image digests")
    expected = campaign.committee_sizes
    actual = [item.get("nodes") for item in state.get("committees", [])]
    if actual != expected:
        raise RuntimeError(
            f"resume committee set differs: state has {actual}, config has {expected}")
    for committee in state["committees"]:
        if committee.get("status") != "completed":
            committee["status"] = "pending"
            committee["error"] = None
    state["status"] = "running"
    state["error"] = None
    state["finished_at"] = None
    return state


_COLUMNS = (
    "nodes", "variant", "protocol", "rate", "adversarial_rate",
    "tps_median", "tps_min",
    "material_p50_ms_since_start", "material_p99_ms_since_start",
    "cpu_cores_p50", "wire_mb_per_s_p50", "bandwidth_efficiency_p50",
    "estimated_non_payload_bytes_per_tx_p50",
)


def _write_results(out: pathlib.Path, campaign: CampaignConfig,
                   groups: list[MatrixGroup], state: dict) -> None:
    """Combine completed point summaries into paper-friendly files."""
    rows = []
    for nodes, _child, configs in groups:
        for variant, _cfg in configs:
            path = out / f"n-{nodes}" / variant / "sweep.json"
            if not path.is_file():
                continue
            sweep = json.loads(path.read_text())
            for point in sweep.get("points", []):
                rows.append({
                    "nodes": nodes,
                    "variant": variant,
                    **{key: point.get(key) for key in _COLUMNS
                       if key not in {"nodes", "variant"}},
                })

    (out / "points.json").write_text(json.dumps(rows, indent=2, allow_nan=False))
    with (out / "points.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=_COLUMNS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)

    status_by_nodes = {
        item["nodes"]: item["status"] for item in state.get("committees", [])
    }
    lines = [
        f"# {campaign.name}",
        "",
        f"Committees: {', '.join(str(n) for n in campaign.committee_sizes)}. "
        f"Status: {state.get('status', 'unknown')}.",
        "",
        "| n | variant | adversarial tx/s | committed tx/s | p50 ms | CPU cores/node | "
        "wire MB/s/node | wire B/sequenced B | non-payload B/tx |",
        "|---:|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in rows:
        lines.append(
            f"| {row['nodes']} | {row['variant']} | "
            f"{_display(row.get('adversarial_rate'), 0)} | "
            f"{_display(row.get('tps_median'), 1)} | "
            f"{_display(row.get('material_p50_ms_since_start'), 1)} | "
            f"{_display(row.get('cpu_cores_p50'), 3)} | "
            f"{_display(row.get('wire_mb_per_s_p50'), 3)} | "
            f"{_display(row.get('bandwidth_efficiency_p50'), 3)} | "
            f"{_display(row.get('estimated_non_payload_bytes_per_tx_p50'), 1)} |"
        )
    if not rows:
        lines.append(
            "| -- | No completed points | -- | -- | -- | -- | -- | -- | -- |"
        )
    lines += ["", "## Fleet status", ""]
    lines += [f"- n={nodes}: {status_by_nodes.get(nodes, 'pending')}"
              for nodes in campaign.committee_sizes]
    (out / "README.md").write_text("\n".join(lines) + "\n")


def _display(value, digits: int) -> str:
    if value is None:
        return "--"
    if isinstance(value, (int, float)):
        return f"{value:.{digits}f}"
    return str(value)


def execute_matrix(campaign: CampaignConfig, groups: list[MatrixGroup],
                   outdir: str, resume: bool = False) -> dict:
    """Run independently provisioned committees in increasing size order."""
    out = pathlib.Path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    state_path = out / "matrix.json"
    if not state_path.exists() and any(out.iterdir()):
        raise RuntimeError(
            f"campaign output is not empty and has no matrix state file: {out}")
    state = _load_state(state_path, campaign, groups, resume)
    _checkpoint(state_path, state)

    try:
        for (nodes, child, configs), entry in zip(groups, state["committees"]):
            if entry["status"] == "completed":
                continue
            entry["status"] = "running"
            _checkpoint(state_path, state)
            child_out = out / entry["output"]
            try:
                execute(
                    child,
                    configs,
                    str(child_out),
                    resume=(resume and (child_out / "campaign.json").is_file()),
                )
            except BaseException as exc:
                entry["status"] = "failed"
                entry["error"] = f"{type(exc).__name__}: {exc}"
                raise
            else:
                entry["status"] = "completed"
                entry["error"] = None
                _checkpoint(state_path, state)
                _write_results(out, campaign, groups, state)
                print(f"campaign: completed n={nodes}; fleet torn down", flush=True)
    except BaseException as exc:
        state["status"] = "failed"
        state["error"] = f"{type(exc).__name__}: {exc}"
        _checkpoint(state_path, state)
        _write_results(out, campaign, groups, state)
        raise

    state["status"] = "completed"
    state["error"] = None
    state["finished_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    _checkpoint(state_path, state)
    _write_results(out, campaign, groups, state)
    return state

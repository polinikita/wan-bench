"""Export Prometheus range-query time series for fault runs.

The two-point scrape delta in `collect` averages a mid-run fault away and is
poisoned by counter resets on restarted nodes. This module queries the control
host's Prometheus over the whole measured window instead; `rate()` handles
counter resets, so restarted validators re-enter the series cleanly.
"""

from __future__ import annotations

import json
import pathlib
import time
import urllib.parse

from .config import RunConfig
from .protocols import uses_vantage_ports
from .ssh import Host, Ssh

PROM_URL = "http://127.0.0.1:9090"
# Seconds of pre-window context so the first in-window rate sample is defined.
LEAD_S = 30


def _rate_window_s(cfg: RunConfig) -> int:
    """Rate windows need several scrapes; four keeps the dip edges sharp."""
    return max(20, 4 * cfg.prometheus_scrape_interval_s)


def queries(cfg: RunConfig) -> dict[str, str]:
    """PromQL per metric role, keyed by series name in the artifact."""
    w = _rate_window_s(cfg)
    if uses_vantage_ports(cfg.protocol):
        counter = 'committed_transactions{node=~"node-[0-9]+-worker-0"}'
        up_sel = 'up{node=~"node-[0-9]+-worker-0"}'
        extra = {
            "cursor_view_min": "min(vantage_cursor_next_view)",
            "cursor_view_max": "max(vantage_cursor_next_view)",
            "seals_by_route": f"sum by (route) (increase(vantage_seals[{w}s]))",
            "skip_votes": f"sum(increase(vantage_skip_votes_received[{w}s]))",
            "install_completed": "sum(vantage_sequence_install_completed_total)",
        }
    else:
        counter = 'sequenced_transactions_total{node=~"node-[0-9]+"}'
        up_sel = 'up{node=~"node-[0-9]+"}'
        extra = {
            "commit_index_min": "min(commit_index)",
            "commit_index_max": "max(commit_index)",
        }
    per_node = f"sum by (node) (rate({counter}[{w}s]))"
    return {
        # Every validator commits the whole replicated stream, so the
        # committee rate is the cross-node median, not the sum.
        "committee_tps_p50": f"quantile(0.5, {per_node})",
        "per_node_tps": per_node,
        "live_validators": f"count({up_sel} == 1)",
        **extra,
    }


def _query_range(ssh: Ssh, control: Host, query: str,
                 start_s: float, end_s: float, step_s: int) -> list:
    params = urllib.parse.urlencode({
        "query": query,
        "start": f"{start_s:.0f}",
        "end": f"{end_s:.0f}",
        "step": str(step_s),
    })
    cmd = f"curl -fsS --max-time 30 '{PROM_URL}/api/v1/query_range?{params}'"
    payload = json.loads(ssh.run(control, cmd))
    if payload.get("status") != "success":
        raise RuntimeError(f"prometheus query_range failed: {payload}")
    return payload["data"]["result"]


def dump(ssh: Ssh, cfg: RunConfig, control: Host, outdir: str,
         end_s: float | None = None) -> pathlib.Path:
    """Write `timeseries.json` covering the measured window; returns the path."""
    end_s = time.time() if end_s is None else end_s
    if cfg.metrics_active_at_ms is not None:
        start_s = cfg.metrics_active_at_ms / 1000.0 - LEAD_S
    else:
        start_s = end_s - cfg.duration_s - LEAD_S
    step_s = cfg.prometheus_scrape_interval_s
    series: dict[str, list] = {}
    errors: dict[str, str] = {}
    for name, query in queries(cfg).items():
        try:
            series[name] = _query_range(ssh, control, query, start_s, end_s, step_s)
        except Exception as exc:  # noqa: BLE001 -- partial series beat none
            errors[name] = str(exc)
            print(f"timeseries: WARNING {name} failed: {exc}", flush=True)
    artifact = {
        "protocol": cfg.protocol,
        "nodes": cfg.nodes,
        "rate": cfg.rate,
        "adversarial_rate": cfg.adversarial_rate,
        "start_s": round(start_s, 3),
        "end_s": round(end_s, 3),
        "step_s": step_s,
        "metrics_active_at_ms": cfg.metrics_active_at_ms,
        "queries": queries(cfg),
        "series": series,
        "errors": errors,
    }
    out = pathlib.Path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / "timeseries.json"
    path.write_text(json.dumps(artifact, indent=2))
    print(f"timeseries: wrote {len(series)} series to {path}", flush=True)
    return path

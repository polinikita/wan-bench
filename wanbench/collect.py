"""Collect and reduce two Prometheus snapshots from every validator."""

from __future__ import annotations

import concurrent.futures
import json
import math
import pathlib
import re
import statistics
import subprocess
import time

from . import diagnostics
from .config import RunConfig
from .ssh import Host, Ssh

METRICS = {
    "vantage": {
        "committed": "committed_transactions",
        "committed_bytes": "committed_bytes",
        # Node-side measurement clock.
        "active_seconds": "metrics_active_seconds",
        "lat_ordering": "transaction_committed_latency",
        "lat_material": "transaction_materialised_latency",
        "cpu": "process_cpu_seconds_total",
        "rss": "process_resident_memory_bytes",
        "bytes_sent": "bytes_sent_total",
        "msgs_sent": "network_messages_sent_total",
        "latency_to_ms": 0.001,
    },
    "starfish": {
        "committed": "sequenced_transactions_total",
        "committed_bytes": "sequenced_transactions_bytes",
        "active_seconds": "benchmark_duration",
        "lat_ordering": "transaction_committed_latency",
        "lat_material": "transaction_committed_latency",
        "cpu": "process_cpu_seconds_total",
        "rss": "process_resident_memory_bytes",
        "bytes_sent": "bytes_sent_total",
        "msgs_sent": "network_requests_sent_total",
        "latency_to_ms": 0.001,
    },
}

# All Vantage-binary protocols share metric definitions.
for _alias in ("autobahn-seamless", "autobahn-optimistic",
               "simple-it", "simple-it-bracha"):
    METRICS[_alias] = METRICS["vantage"]


def _metrics_ports(cfg: RunConfig, node: Host) -> list[int]:
    """Return every metrics port for one node."""
    from .protocols import VANTAGE_PORTS, uses_vantage_ports
    if uses_vantage_ports(cfg.protocol):
        return [VANTAGE_PORTS["worker_metrics"], VANTAGE_PORTS["primary_metrics"]]
    # Starfish assigns metrics port 1500+n+i.
    return [1500 + cfg.nodes + node.index]


def _scrape_one(ssh: Ssh, cfg: RunConfig, control: Host, node: Host) -> str:
    # Mark each endpoint so strict collection can distinguish zero from unavailable.
    ports = _metrics_ports(cfg, node)
    commands = []
    for port in ports:
        url = f"http://{node.private_ip}:{port}/metrics"
        commands.append(
            f"if body=$(curl -fsS --max-time 10 {url}); then "
            f"printf '# WANBENCH_OK {port}\\n%s\\n' \"$body\"; else "
            f"printf '# WANBENCH_FAILED {port}\\n'; fi")
    cmd = "; ".join(commands)
    # Cover every per-port curl timeout plus SSH setup.
    ssh_timeout = 30 + 10 * len(ports)
    try:
        return ssh.run(control, cmd, timeout=ssh_timeout, check=False)
    except (subprocess.TimeoutExpired, OSError):
        return "".join(f"# WANBENCH_FAILED {port}\n" for port in ports)


def scrape_all(ssh: Ssh, cfg: RunConfig, control: Host, hosts: list[Host]) -> dict[int, str]:
    # Parallel scrapes reduce snapshot skew.
    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
        texts = pool.map(lambda h: _scrape_one(ssh, cfg, control, h), hosts)
    return {h.index: t for h, t in zip(hosts, texts)}


SCRAPE_RETRY_ATTEMPTS = 3
SCRAPE_RETRY_DELAY_S = 10.0


def _hosts_missing_ports(cfg: RunConfig, hosts: list[Host],
                         snapshot: dict[int, str]) -> list[Host]:
    return [host for host in hosts
            if set(_metrics_ports(cfg, host)) - _successful_ports(snapshot.get(host.index, ""))]


def _scrape_with_retry(ssh: Ssh, cfg: RunConfig, control: Host, hosts: list[Host],
                       attempts: int = SCRAPE_RETRY_ATTEMPTS,
                       delay_s: float = SCRAPE_RETRY_DELAY_S) -> dict[int, str]:
    """Retry only hosts with missing endpoint markers."""
    snapshot = scrape_all(ssh, cfg, control, hosts)
    pending = _hosts_missing_ports(cfg, hosts, snapshot)
    for _ in range(attempts - 1):
        if not pending:
            break
        time.sleep(delay_s)
        snapshot.update(scrape_all(ssh, cfg, control, pending))
        pending = _hosts_missing_ports(cfg, hosts, snapshot)
    return snapshot


PROGRESS_TIMEOUT_S = 120
PROGRESS_POLL_S = 10
STRICT_MIN_NODE_RATE_PCT_OF_MEDIAN = 80.0


def wait_for_progress(ssh: Ssh, cfg: RunConfig, control: Host, hosts: list[Host],
                      timeout_s: int = PROGRESS_TIMEOUT_S,
                      poll_s: int = PROGRESS_POLL_S) -> list[Host]:
    """Wait for every node's committed counter to increase.

    The timeout must include the metrics gate delay. The caller validates rate and
    cursor lag separately and retries the complete committee when nodes stall.
    """
    family = METRICS[cfg.protocol]["committed"]
    snapshot = scrape_all(ssh, cfg, control, hosts)
    baseline = {h.index: _family_sum(snapshot.get(h.index, ""), family) for h in hosts}
    pending = {h.index: h for h in hosts}
    deadline = time.monotonic() + timeout_s

    while pending and time.monotonic() < deadline:
        time.sleep(poll_s)
        snapshot = scrape_all(ssh, cfg, control, list(pending.values()))
        for index in list(pending):
            if _family_sum(snapshot.get(index, ""), family) > baseline[index]:
                del pending[index]
        print(f"progress: {len(hosts) - len(pending)}/{len(hosts)} node(s) advancing "
              f"({family})", flush=True)

    return [pending[i] for i in sorted(pending)]


def _family_sum(text: str, family: str) -> float:
    total = 0.0
    for m in re.finditer(rf'^{re.escape(family)}(?:{{[^}}]*}})? (\S+)$', text, re.M):
        try:
            total += float(m.group(1))
        except ValueError:
            pass
    return total


def _family_by_label(text: str, family: str, label: str) -> dict[str, float]:
    """Sum a metric family by one label value."""
    out: dict[str, float] = {}
    pattern = rf'^{re.escape(family)}\{{[^}}]*{re.escape(label)}="([^"]*)"[^}}]*}} (\S+)$'
    for m in re.finditer(pattern, text, re.M):
        try:
            out[m.group(1)] = out.get(m.group(1), 0.0) + float(m.group(2))
        except ValueError:
            pass
    return out


def _family_values(text: str, family: str) -> list[float]:
    """Return every value without reducing across processes."""
    values = []
    for m in re.finditer(rf'^{re.escape(family)}(?:{{[^}}]*}})? (\S+)$', text, re.M):
        try:
            values.append(float(m.group(1)))
        except ValueError:
            pass
    return values


def _gauge_v(text: str, family: str, v: str) -> float | None:
    m = re.search(rf'^{re.escape(family)}{{[^}}]*v="{v}"[^}}]*}} (\S+)', text, re.M)
    return float(m.group(1)) if m else None


def _successful_ports(text: str) -> set[int]:
    return {int(port) for port in re.findall(r"^# WANBENCH_OK (\d+)$", text, re.M)}


def _has_family(text: str, family: str) -> bool:
    return re.search(rf"^{re.escape(family)}(?:{{[^}}]*}})?\s+\S+", text, re.M) is not None


def _validate_snapshot(cfg: RunConfig, hosts: list[Host], snapshot: dict[int, str],
                       stage: str) -> None:
    errors = []
    committed = METRICS[cfg.protocol]["committed"]
    for host in hosts:
        text = snapshot.get(host.index, "")
        expected = set(_metrics_ports(cfg, host))
        missing = sorted(expected - _successful_ports(text))
        if missing:
            errors.append(f"node {host.index} endpoints {missing} unavailable")
        elif not _has_family(text, committed):
            errors.append(f"node {host.index} missing {committed}")
    if errors:
        details = "; ".join(errors)
        raise RuntimeError(f"invalid {stage} metrics snapshot: {details}")


def check_progress_quality(ssh: Ssh, cfg: RunConfig, control: Host, hosts: list[Host],
                           min_rate_pct: float = 25.0, max_cursor_lag: int = 200,
                           sample_s: int = 10, max_boot_spread_s: float = 3.0,
                           min_client_lead_s: float = 10.0,
                           enforce_rate_and_lag: bool = True) -> None:
    """Check startup timing, commit rate, and Vantage cursor lag."""
    family = METRICS[cfg.protocol]["committed"]
    first = _scrape_with_retry(ssh, cfg, control, hosts, attempts=1)
    t0 = time.monotonic()
    time.sleep(sample_s)
    second = _scrape_with_retry(ssh, cfg, control, hosts, attempts=1)
    window = max(time.monotonic() - t0, 1e-6)

    rates = [
        (h.index,
         (_family_sum(second.get(h.index, ""), family)
          - _family_sum(first.get(h.index, ""), family)) / window)
        for h in hosts
    ]
    median_rate = statistics.median(rate for _, rate in rates) if rates else 0.0
    slowest_node, min_rate = min(rates, key=lambda item: item[1], default=(-1, 0.0))
    # Every node commits the aggregate replicated stream.
    floor = cfg.rate * min_rate_pct / 100.0
    print(f"progress: committed rate median {median_rate:,.0f}, "
          f"min {min_rate:,.0f} (node {slowest_node}) tx/s over {window:.0f}s "
          f"(floor {floor:,.0f} = {min_rate_pct:.0f}% of {cfg.rate:,} offered)", flush=True)

    starts = [
        (h.index, min(values))
        for h in hosts
        if (values := _family_values(second.get(h.index, ""),
                                     "process_start_time_seconds"))
    ]
    if len(starts) == len(hosts):
        earliest = min(start for _, start in starts)
        latest_node, latest = max(starts, key=lambda item: item[1])
        spread = latest - earliest
        print(f"progress: boot spread {spread:.1f}s; limit {max_boot_spread_s:.1f}s",
              flush=True)
        if spread > max_boot_spread_s:
            raise RuntimeError(
                f"validator boot spread {spread:.1f}s exceeds "
                f"{max_boot_spread_s:.1f}s")
        if cfg.client_activate_at_ms is not None:
            lead = cfg.client_activate_at_ms / 1_000 - latest
            print(f"progress: client lead {lead:.1f}s after latest validator; "
                  f"minimum {min_client_lead_s:.1f}s", flush=True)
            if lead < min_client_lead_s:
                raise RuntimeError(
                    f"client activation leads latest validator by only {lead:.1f}s "
                    f"on node {latest_node}; minimum is {min_client_lead_s:.1f}s")

    if min_rate < floor:
        slow = [index for index, rate in rates if rate < floor]
        message = (
            f"node(s) {slow} passed the barrier but commit below "
            f"{min_rate_pct:.0f}% of the offered {cfg.rate:,} tx/s "
            f"(slowest {min_rate:,.0f} tx/s on node {slowest_node})")
        if enforce_rate_and_lag:
            raise RuntimeError(message)
        print(f"progress: WARNING {message}", flush=True)

    if cfg.protocol != "vantage":
        return
    lags = []
    for h in hosts:
        text = second.get(h.index, "")
        entered = _family_sum(text, "vantage_entered_view")
        cursor = _family_sum(text, "vantage_cursor_next_view")
        if entered > 0:
            lags.append((h.index, int(entered - cursor)))
    if not lags:
        return
    worst_node, worst = max(lags, key=lambda p: p[1])
    med = statistics.median([v for _, v in lags])
    print(f"progress: cursor lag median {med:.0f}, worst {worst} (node {worst_node}); "
          f"limit {max_cursor_lag}", flush=True)
    if worst > max_cursor_lag:
        behind = sorted((i for i, v in lags if v > max_cursor_lag))
        message = (
            f"node(s) {behind} have an output cursor more than {max_cursor_lag} views "
            f"behind their entered view (worst {worst} on node {worst_node}) -- AGB is "
            f"advancing without producing committed output")
        if enforce_rate_and_lag:
            raise RuntimeError(message)
        print(f"progress: WARNING {message}", flush=True)


def dump_failure_scrapes(ssh: Ssh, cfg: RunConfig, control: Host,
                         hosts: list[Host], outdir: str, tag: str) -> None:
    """Best-effort raw scrape capture for a failed point."""
    try:
        out = pathlib.Path(outdir)
        out.mkdir(parents=True, exist_ok=True)
        snap = _scrape_with_retry(ssh, cfg, control, hosts, attempts=1)
        for i, txt in snap.items():
            (out / f"{tag}-node-{i}.prom").write_text(txt)
        print(f"collect: wrote {len(snap)} {tag} scrape(s) to {out}", flush=True)
        if not any(out.glob("final-node-*.prom")):
            diagnostics.capture_nodes(ssh, hosts, out, tag)
    except Exception as exc:  # noqa: BLE001 -- must never mask the real failure
        print(f"collect: could not capture {tag} scrapes: {exc}", flush=True)


def _wait_for_window_s(now_ms: float, metrics_active_at_ms: int | None,
                       margin_s: float = 2.0) -> float:
    """Return the delay needed to place the baseline after the metrics gate."""
    if metrics_active_at_ms is None:
        return 0.0
    remaining = (metrics_active_at_ms - now_ms) / 1000.0
    return remaining + margin_s if remaining > 0 else 0.0


def collect(ssh: Ssh, cfg: RunConfig, control: Host, hosts: list[Host],
            outdir: str, baseline_at: int = 15, final_at: int | None = None,
            strict: bool = False) -> dict:
    """Run the timed scrape pair and write raw + summary artifacts."""
    final_at = (max(baseline_at + 30, cfg.duration_s - 10)
                if final_at is None else final_at)
    if baseline_at < 0 or final_at <= baseline_at:
        raise ValueError(
            f"collect timing needs 0 <= baseline_at < final_at, got "
            f"{baseline_at}, {final_at}")
    out = pathlib.Path(outdir)
    out.mkdir(parents=True, exist_ok=True)

    t0 = time.monotonic()
    time.sleep(max(0, baseline_at))
    # Delay only when the requested baseline precedes the metrics gate.
    extra = _wait_for_window_s(time.time() * 1000, cfg.metrics_active_at_ms)
    if extra > 0:
        print(f"collect: baseline would precede the metrics-active window; "
              f"waiting a further {extra:.1f}s so it observes a non-empty population "
              f"(final scrape pushed out to keep the {final_at - baseline_at}s window)",
              flush=True)
        time.sleep(extra)
        # Preserve the requested interval between snapshots.
        final_at += extra
    base = _scrape_with_retry(ssh, cfg, control, hosts)
    base_ts = time.monotonic()
    for i, txt in base.items():
        (out / f"baseline-node-{i}.prom").write_text(txt)
    if strict:
        _validate_snapshot(cfg, hosts, base, "baseline")

    time.sleep(max(0, final_at - (time.monotonic() - t0)))
    fin = _scrape_with_retry(ssh, cfg, control, hosts)
    fin_ts = time.monotonic()
    for i, txt in fin.items():
        (out / f"final-node-{i}.prom").write_text(txt)
    if ssh is not None:
        diagnostics.capture_nodes(ssh, hosts, out)
    if strict:
        _validate_snapshot(cfg, hosts, fin, "final")

    # Read netem drops after benchmark traffic.
    netem_dropped_packets = None
    if cfg.wan.mode == "netem":
        try:
            from . import prepare
            netem_dropped_packets = prepare.report_netem_drops(ssh, hosts)
        except Exception as exc:  # noqa: BLE001 -- observability must not sink a run
            print(f"wan: post-run netem drop read failed: {exc}", flush=True)

    wall_window = fin_ts - base_ts
    m = METRICS[cfg.protocol]

    def delta(i: int, fam: str) -> float:
        return _family_sum(fin[i], fam) - _family_sum(base[i], fam)

    indices = [host.index for host in hosts]

    # Use each node's active clock. Take max across processes to avoid double counting.
    # Fall back to the wall interval when the node does not publish an active clock.
    def active_delta(i: int) -> float:
        fin_values = _family_values(fin[i], m["active_seconds"])
        base_values = _family_values(base[i], m["active_seconds"])
        return (max(fin_values) if fin_values else 0.0) - (
            max(base_values) if base_values else 0.0)

    active_deltas = {i: active_delta(i) for i in indices}
    node_window = {i: (d if d > 0 else wall_window) for i, d in active_deltas.items()}
    used_node_clock = any(d > 0 for d in active_deltas.values())
    # Report a representative window while retaining per-node rate denominators.
    window = statistics.median(node_window.values()) if node_window else wall_window
    committed = [delta(i, m["committed"]) for i in indices]
    committed_bytes = [delta(i, m["committed_bytes"]) for i in indices]
    if strict and any(value < 0 for value in committed):
        raise RuntimeError(
            f"committed counter reset during measurement: {committed}")
    # A crashed process loses its counters and a restarted one recounts from
    # zero over a partial window, so two-point deltas for the crash cohort are
    # meaningless (positive but short, or negative). Exclude the configured
    # crash nodes, plus any node whose counter visibly reset, from every
    # reduction below. timeseries.json carries their recovery curves.
    fault_dead = set(cfg.fault.nodes) if cfg.fault.kind == "crash" else set()
    reset_set = fault_dead | {i for i, v in zip(indices, committed) if v < 0}
    reset_nodes = sorted(reset_set)
    live = [i for i in indices if i not in reset_set]
    committed = [c for i, c in zip(indices, committed) if i not in reset_set]
    committed_bytes = [c for i, c in zip(indices, committed_bytes)
                       if i not in reset_set]
    if reset_nodes:
        print(f"collect: node(s) {reset_nodes} crashed or reset mid-run; "
              f"medians use the {len(live)} remaining node(s)", flush=True)
    # Strict sweeps require every node to commit during the window.
    if strict:
        stalled = [i for i, value in zip(indices, committed) if value == 0]
        if stalled:
            raise RuntimeError(
                f"node(s) {stalled} committed nothing during measurement "
                f"(committed deltas: {committed})")
    # Reduce per-node rates after applying each node's own denominator.
    def per_node_rate(i: int, value: float) -> float:
        w = node_window[i]
        return value / w if w else 0.0

    node_tps = [per_node_rate(i, c) for i, c in zip(live, committed)]
    tps = statistics.median(node_tps) if node_tps else 0.0
    if strict and node_tps:
        floor = tps * STRICT_MIN_NODE_RATE_PCT_OF_MEDIAN / 100.0
        slow = [(i, round(rate, 1)) for i, rate in zip(live, node_tps) if rate < floor]
        if slow:
            raise RuntimeError(
                f"node commit rates below {STRICT_MIN_NODE_RATE_PCT_OF_MEDIAN:.0f}% "
                f"of committee median {tps:.1f} tx/s: {slow}")

    def lat(snapshot: dict[int, str], fam: str, v: str) -> float:
        vals = [x for i in live if (x := _gauge_v(snapshot[i], fam, v)) is not None]
        return statistics.median(vals) if vals else float("nan")

    cpu = [per_node_rate(i, delta(i, m["cpu"])) * 100 for i in live]
    rss = [_family_sum(fin[i], m["rss"]) / 1e6 for i in live]
    # Sum per-node wire rates after applying per-node windows.
    sent_bytes = [delta(i, m["bytes_sent"]) for i in live]
    node_bytes_rate = [per_node_rate(i, value)
                       for i, value in zip(live, sent_bytes)]
    bytes_sent_rate = sum(node_bytes_rate)
    msgs_sent_rate = sum(per_node_rate(i, delta(i, m["msgs_sent"])) for i in live)
    # Per-node median is comparable across committee sizes; fleet total is not.
    wire_p50 = statistics.median(node_bytes_rate) if node_bytes_rate else 0.0

    # Per-type medians are independent and need not sum to the total median.
    wire_types: dict[str, list[float]] = {}
    for i in live:
        fin_by = _family_by_label(fin[i], "network_bytes_sent_total", "type")
        base_by = _family_by_label(base[i], "network_bytes_sent_total", "type")
        w = node_window[i]
        if not w:
            continue
        for t in set(fin_by) | set(base_by):
            wire_types.setdefault(t, []).append(
                (fin_by.get(t, 0.0) - base_by.get(t, 0.0)) / w)
    # Omit types below 1 kB/s.
    wire_by_type = {
        t: round(v / 1e6, 4)
        for t, vals in wire_types.items()
        if (v := statistics.median(vals)) >= 1_000
    }

    def healthy(snapshot: dict[int, str]) -> int:
        return sum(set(_metrics_ports(cfg, host)) <= _successful_ports(snapshot[host.index])
                   for host in hosts)

    summary = {
        "run_id": cfg.run_id, "protocol": cfg.protocol, "nodes": cfg.nodes,
        "rate": cfg.rate, "adversarial_rate": cfg.adversarial_rate,
        "delta_ms": cfg.delta_ms, "fault": cfg.fault.kind,
        "fault_nodes": sorted(cfg.fault.nodes) if cfg.fault.kind != "none" else [],
        # Crash cohort plus any counter-reset node; excluded from all medians.
        "excluded_nodes": reset_nodes,
        "nodes_in_medians": len(live),
        # None means mimic mode or unavailable counters.
        "netem_dropped_packets": netem_dropped_packets,
        # Median per-node measurement duration.
        "window_s": round(window, 1),
        "window_source": "node_clock" if used_node_clock else "wall_clock",
        "wall_window_s": round(wall_window, 1),
        "healthy_nodes_baseline": healthy(base),
        "healthy_nodes_final": healthy(fin),
        "tps_median": round(tps, 1),
        "tps_min": round(min(node_tps), 1) if node_tps else 0.0,
        # Latency reporters are cumulative from process start.
        "ordering_p50_ms_since_start": _msround(
            lat(fin, m["lat_ordering"], "p50"), m["latency_to_ms"]),
        "ordering_p90_ms_since_start": _msround(
            lat(fin, m["lat_ordering"], "p90"), m["latency_to_ms"]),
        "ordering_p95_ms_since_start": _msround(
            lat(fin, m["lat_ordering"], "p95"), m["latency_to_ms"]),
        "ordering_p99_ms_since_start": _msround(
            lat(fin, m["lat_ordering"], "p99"), m["latency_to_ms"]),
        "ordering_p50_ms_at_baseline": _msround(
            lat(base, m["lat_ordering"], "p50"), m["latency_to_ms"]),
        # Materialised latency includes local payload availability.
        "material_p50_ms_since_start": _msround(
            lat(fin, m["lat_material"], "p50"), m["latency_to_ms"]),
        "material_p90_ms_since_start": _msround(
            lat(fin, m["lat_material"], "p90"), m["latency_to_ms"]),
        "material_p95_ms_since_start": _msround(
            lat(fin, m["lat_material"], "p95"), m["latency_to_ms"]),
        "material_p99_ms_since_start": _msround(
            lat(fin, m["lat_material"], "p99"), m["latency_to_ms"]),
        "material_p50_ms_at_baseline": _msround(
            lat(base, m["lat_material"], "p50"), m["latency_to_ms"]),
        "cpu_pct_median": round(statistics.median(cpu), 1),
        "cpu_cores_p50": round(statistics.median(cpu) / 100, 3),
        "rss_mb_median": round(statistics.median(rss), 1),
        "wire_mb_per_s": round(bytes_sent_rate / 1e6, 2),
        "wire_mb_per_s_p50": round(wire_p50 / 1e6, 2),
        **_bandwidth_fields(cfg, committed, committed_bytes, sent_bytes),
        # Per-type values are sorted by descending bandwidth.
        "wire_mb_per_s_by_type": dict(sorted(wire_by_type.items(),
                                             key=lambda kv: -kv[1])),
        "wire_kmsg_per_s": round(msgs_sent_rate / 1e3, 1),
        # Protocol health fields are empty or zero when unsupported.
        **_straggler_fields(cfg, fin, base, live, delta),
        **_worker_health_fields(cfg, fin, base, live, window),
        **_starfish_memory_fields(cfg, fin, live),
    }
    encoded = json.dumps(summary, indent=2, allow_nan=False)
    (out / "summary.json").write_text(encoded)
    print(encoded)
    return summary


def _msround(v: float, latency_to_ms: float) -> float | None:
    if not math.isfinite(v):
        return None
    return round(v * latency_to_ms, 1)


def _bandwidth_fields(cfg: RunConfig, committed: list[float],
                      committed_bytes: list[float],
                      sent_bytes: list[float]) -> dict:
    """Return per-validator wire cost normalized by committed payload."""
    payload_factor = (cfg.nodes - 1) / cfg.nodes
    rows = [
        (tx, payload, sent)
        for tx, payload, sent in zip(committed, committed_bytes, sent_bytes)
        if tx > 0 and payload > 0 and sent >= 0
    ]
    if not rows:
        return {
            "committed_bytes_per_tx_p50": None,
            "wire_bytes_per_tx_p50": None,
            "bandwidth_efficiency_p50": None,
            "estimated_payload_efficiency": round(payload_factor, 4),
            "estimated_non_payload_bytes_per_tx_p50": None,
            "estimated_non_payload_efficiency_p50": None,
        }

    payload_per_tx = [payload / tx for tx, payload, _sent in rows]
    wire_per_tx = [sent / tx for tx, _payload, sent in rows]
    efficiency = [sent / payload for _tx, payload, sent in rows]
    non_payload_per_tx = [
        max(sent - payload * payload_factor, 0.0) / tx
        for tx, payload, sent in rows
    ]
    non_payload_efficiency = [
        max(sent / payload - payload_factor, 0.0)
        for _tx, payload, sent in rows
    ]
    return {
        "committed_bytes_per_tx_p50": round(statistics.median(payload_per_tx), 1),
        "wire_bytes_per_tx_p50": round(statistics.median(wire_per_tx), 1),
        "bandwidth_efficiency_p50": round(statistics.median(efficiency), 4),
        "estimated_payload_efficiency": round(payload_factor, 4),
        "estimated_non_payload_bytes_per_tx_p50": round(
            statistics.median(non_payload_per_tx), 1),
        "estimated_non_payload_efficiency_p50": round(
            statistics.median(non_payload_efficiency), 4),
    }


def _starfish_memory_fields(cfg: RunConfig, fin: dict[int, str],
                            indices: list[int]) -> dict:
    if cfg.protocol != "starfish":
        return {}

    blocks = [_family_sum(fin[i], "dag_blocks_in_memory") for i in indices]
    serialized_mb = [
        _family_sum(fin[i], "global_in_memory_blocks_bytes") / 1e6
        for i in indices
    ]
    unloaded = [_family_sum(fin[i], "dag_state_unloaded_blocks") for i in indices]
    return {
        "dag_blocks_in_memory_p50": round(statistics.median(blocks), 1),
        "dag_serialized_mb_in_memory_p50": round(statistics.median(serialized_mb), 1),
        "dag_serialized_mb_in_memory_max": round(max(serialized_mb), 1),
        "dag_state_unloaded_blocks_p50": round(statistics.median(unloaded), 1),
    }


def _split_by_port(text: str) -> dict[int, str]:
    """Split one node's concatenated scrape by metrics port."""
    out: dict[int, str] = {}
    port: int | None = None
    buf: list[str] = []
    for line in text.splitlines(keepends=True):
        m = re.match(r"#\s*WANBENCH_(?:OK|FAILED)\s+(\d+)\s*$", line)
        if m:
            if port is not None:
                out[port] = "".join(buf)
            port, buf = int(m.group(1)), []
            continue
        buf.append(line)
    if port is not None:
        out[port] = "".join(buf)
    return out


def _worker_health_fields(cfg: RunConfig, fin: dict[int, str], base: dict[int, str],
                          indices: list[int], window: float) -> dict:
    """Reduce Vantage-binary worker health metrics for summary.json.

    Queue and heartbeat fields use committee maxima. Store drain uses the worker-only
    minimum so one stalled worker is not hidden by healthy primaries.
    """
    from .protocols import uses_vantage_ports, VANTAGE_PORTS
    if not uses_vantage_ports(cfg.protocol):
        return {}

    # Record committee maxima and the largest walk-to-block ratio.
    walk: dict[str, float] = {}
    worst_ratio = 0.0
    for i in indices:
        for fam, v in _family_by_label(fin[i], "vantage_walk_steps_total", "family").items():
            base_v = _family_by_label(base.get(i, ""), "vantage_walk_steps_total",
                                     "family").get(fam, 0.0)
            walk[fam] = max(walk.get(fam, 0.0), v - base_v)
        total = sum(_family_by_label(fin[i], "vantage_walk_steps_total", "family").values()) \
            - sum(_family_by_label(base.get(i, ""), "vantage_walk_steps_total", "family").values())
        blocks = _family_sum(fin[i], "vantage_blocks_received") \
            - _family_sum(base.get(i, ""), "vantage_blocks_received")
        if blocks > 0:
            worst_ratio = max(worst_ratio, total / blocks)

    peaks: dict[str, float] = {}
    for i in indices:
        for stage, value in _family_by_label(fin[i], "worker_queue_peak", "queue").items():
            peaks[stage] = max(peaks.get(stage, 0.0), value)

    # Each process publishes its own store age; use the maximum, not the sum.
    ages = [max(v) for i in indices
            if (v := _family_values(fin[i], "store_actor_heartbeat_age_ms"))]

    # process_panics is a running-total gauge.
    panics = sum(_family_sum(fin[i], "process_panics") for i in indices)

    age_peaks = [max(v) for i in indices
                 if (v := _family_values(fin[i], "store_actor_heartbeat_age_ms_peak"))]
    pending_keys = [_family_sum(fin[i], "vantage_pending_payload_keys") for i in indices]
    last_sync = [_family_sum(fin[i], "vantage_last_synchronize_len") for i in indices]

    # Read store drain only from the worker process.
    worker_port = VANTAGE_PORTS["worker_metrics"]
    drain_rates = []
    for i in indices:
        fin_w = _split_by_port(fin[i]).get(worker_port, "")
        base_w = _split_by_port(base.get(i, "")).get(worker_port, "")
        if not _has_family(fin_w, "store_commands_drained_total"):
            continue
        delta = (_family_sum(fin_w, "store_commands_drained_total")
                 - _family_sum(base_w, "store_commands_drained_total"))
        drain_rates.append(delta / window if window > 0 else 0.0)

    return {
        "worker_queue_peak_max_by_stage": dict(sorted(peaks.items(),
                                                      key=lambda kv: -kv[1])),
        "store_heartbeat_age_ms_max": round(max(ages), 0) if ages else 0.0,
        "store_heartbeat_age_ms_peak_max": round(max(age_peaks), 0) if age_peaks else 0.0,
        # Minimum exposes one stalled worker.
        "worker_store_drained_per_s_min": (round(min(drain_rates), 1)
                                           if drain_rates else 0.0),
        "worker_store_drained_per_s_median": (round(statistics.median(drain_rates), 1)
                                              if drain_rates else 0.0),
        "pending_payload_keys_max": round(max(pending_keys), 0) if pending_keys else 0.0,
        "last_synchronize_len_max": round(max(last_sync), 0) if last_sync else 0.0,
        "walk_steps_max_by_family": {k: round(v, 0) for k, v in
                                     sorted(walk.items(), key=lambda kv: -kv[1])},
        "walk_steps_per_block_max": round(worst_ratio, 1),
        "panics_total": round(panics, 0),
    }


def _straggler_fields(cfg: RunConfig, fin: dict[int, str], base: dict[int, str],
                      indices: list[int], delta) -> dict:
    """Reduce Vantage-only lag and queue metrics."""
    if cfg.protocol != "vantage":
        return {}
    gate = [_family_sum(fin[i], "vantage_pending_gate_len") for i in indices]
    peak = [_family_sum(fin[i], "core_queue_peak") for i in indices]
    shed = [delta(i, "network_volatile_shed_total") for i in indices]
    bulk = [delta(i, "vantage_bulk_inbound_dropped_total") for i in indices]
    # Use each node's earliest process start to compute launch spread.
    starts = [min(v) for i in indices
              if (v := _family_values(fin[i], "process_start_time_seconds"))]
    return {
        "boot_spread_s": round(max(starts) - min(starts), 1) if starts else None,
        "pending_gate_max": round(max(gate), 0) if gate else 0.0,
        "pending_gate_median": round(statistics.median(gate), 0) if gate else 0.0,
        "core_queue_peak_max": round(max(peak), 0) if peak else 0.0,
        "core_queue_peak_median": round(statistics.median(peak), 0) if peak else 0.0,
        "volatile_shed_total": round(sum(shed), 0),
        "bulk_dropped_total": round(sum(bulk), 0),
    }

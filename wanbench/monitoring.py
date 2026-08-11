"""Run Prometheus and Grafana on the control host."""

from __future__ import annotations

import json
import pathlib
import re
import tempfile
import time
import urllib.request

from .aws import Aws
from .config import RunConfig
from .protocols import VANTAGE_PORTS, uses_vantage_ports
from .ssh import Host, Ssh

GRAFANA_PORT = 3000
PROM_PORT = 9090

# Bound public-IP lookup retries before provisioning.
_MY_IP_ATTEMPTS = 3
_MY_IP_RETRY_DELAY_S = 2


def _my_ip() -> str:
    last_exc: Exception | None = None
    for attempt in range(_MY_IP_ATTEMPTS):
        try:
            ip = urllib.request.urlopen(
                "https://checkip.amazonaws.com", timeout=10).read().decode().strip()
            return f"{ip}/32"
        except Exception as exc:  # noqa: BLE001 -- any transient network error
            last_exc = exc
            if attempt < _MY_IP_ATTEMPTS - 1:
                time.sleep(_MY_IP_RETRY_DELAY_S)
    raise last_exc


DEFAULT_SCRAPE_INTERVAL_S = 30


def _yaml_single_quoted(value: object) -> str:
    return str(value).replace("'", "''")


def validator_targets(cfg: RunConfig, hosts: list[Host],
                      labels: dict[str, str] | None = None) -> list[tuple[str, dict]]:
    """Return labelled Prometheus targets for a protocol."""
    targets: list[tuple[str, dict]] = []
    shared = dict(labels or {})
    for host in hosts:
        if uses_vantage_ports(cfg.protocol):
            targets.append((
                f"{host.private_ip}:{VANTAGE_PORTS['primary_metrics']}",
                {**shared, "node": f"node-{host.index}-primary"},
            ))
            targets.append((
                f"{host.private_ip}:{VANTAGE_PORTS['worker_metrics']}",
                {**shared, "node": f"node-{host.index}-worker-0"},
            ))
        else:
            targets.append((
                f"{host.private_ip}:{1500 + cfg.nodes + host.index}",
                {**shared, "node": f"node-{host.index}"},
            ))
    return targets


def _prometheus_yml(targets, scrape_interval_s: int = DEFAULT_SCRAPE_INTERVAL_S) -> str:
    """Render labelled static Prometheus targets."""
    # The 30-second default limits scrape overhead on large committees.
    if scrape_interval_s < 1:
        raise ValueError("scrape_interval_s must be >= 1")
    interval = f"{scrape_interval_s}s"
    timeout = f"{min(10, scrape_interval_s)}s"
    lines = [f"global:\n  scrape_interval: {interval}\n"
             f"  scrape_timeout: {timeout}\n  evaluation_interval: {interval}\n",
             "scrape_configs:\n  - job_name: validators\n    static_configs:\n"]
    for t in targets:
        tgt, labels = t if isinstance(t, (tuple, list)) else (t, {})
        target = _yaml_single_quoted(tgt)
        lines.append(f"      - targets: ['{target}']\n")
        if labels:
            lines.append("        labels:\n")
            lines += [
                f"          {k}: '{_yaml_single_quoted(v)}'\n"
                for k, v in labels.items()
            ]
    return "".join(lines)


def _datasource_yml() -> str:
    return (
        "apiVersion: 1\n"
        "datasources:\n"
        "  - name: Prometheus\n    uid: prom\n    type: prometheus\n    access: proxy\n"
        f"    url: http://localhost:{PROM_PORT}\n    isDefault: true\n"
    )


def _dashboards_provider_yml() -> str:
    return (
        "apiVersion: 1\n"
        "providers:\n"
        "  - name: wanbench\n    type: file\n    options:\n"
        "      path: /etc/grafana/provisioning/dashboards\n"
    )


def _dashboard_json() -> str:
    def stat(title, unit, targets, x, text_mode="value", w=6):
        return {
            "type": "stat", "title": title,
            "datasource": {"type": "prometheus", "uid": "prom"},
            "gridPos": {"h": 3, "w": w, "x": x, "y": 0},
            "fieldConfig": {"defaults": {"unit": unit}, "overrides": []},
            "options": {
                "colorMode": "value", "graphMode": "none",
                "justifyMode": "auto", "orientation": "auto",
                "reduceOptions": {
                    "calcs": ["lastNotNull"], "fields": "", "values": False,
                },
                "textMode": text_mode,
            },
            "targets": [{"datasource": {"type": "prometheus", "uid": "prom"},
                         "expr": e, "legendFormat": lg, "refId": chr(65 + i),
                         "instant": True}
                        for i, (e, lg) in enumerate(targets)],
        }

    def ts(title, unit, targets, y, x=0, w=24):
        return {
            "type": "timeseries", "title": title,
            "datasource": {"type": "prometheus", "uid": "prom"},
            "gridPos": {"h": 8, "w": w, "x": x, "y": y},
            "fieldConfig": {"defaults": {"unit": unit, "custom": {"fillOpacity": 10}}, "overrides": []},
            "targets": [{"datasource": {"type": "prometheus", "uid": "prom"},
                         "expr": e, "legendFormat": lg, "refId": chr(65 + i)}
                        for i, (e, lg) in enumerate(targets)],
        }

    # Normalize process labels to one series per validator.
    BY_NODE = 'sum by (n) (label_replace({expr}, "n", "$1", "node", "(node-[0-9]+).*"))'

    def by_node(expr):
        return BY_NODE.format(expr=expr)

    def first_present(*expressions):
        return " or ".join(f"({expr})" for expr in expressions)

    def bands(expr):
        return [
            (f"min({expr})", "min"),
            (f"quantile(0.5, {expr})", "p50"),
            (f"max({expr})", "max"),
        ]

    # Queries support both Vantage and Starfish metric names.
    live_validators = (
        'count(count by (n) (label_replace('
        'up{node=~"node-[0-9]+.*"} == 1, "n", "$1", "node", '
        '"(node-[0-9]+).*"))) or vector(0)'
    )
    vantage_tps = by_node(
        'rate(committed_transactions{node=~"node-[0-9]+-worker-0"}[2m])'
    )
    starfish_tps = by_node(
        'rate(sequenced_transactions_total{node=~"node-[0-9]+"}[2m])'
    )
    per_node_tps = first_present(vantage_tps, starfish_tps)
    median_tps = (
        f'quantile(0.5, {vantage_tps}) '
        f'or quantile(0.5, {starfish_tps}) '
        'or vector(0)'
    )

    vantage_progress = by_node(
        'vantage_entered_view{node=~"node-[0-9]+-primary"}'
    )
    starfish_progress = by_node(
        'dag_highest_round{node=~"node-[0-9]+"}'
    )
    progress = first_present(vantage_progress, starfish_progress)
    progress_rate = first_present(
        by_node('clamp_min(deriv(vantage_entered_view{node=~"node-[0-9]+-primary"}[2m]), 0)'),
        by_node('clamp_min(deriv(dag_highest_round{node=~"node-[0-9]+"}[2m]), 0)'),
    )

    vantage_core_busy = by_node(
        'rate(utilization_timer{node=~"node-[0-9]+-primary"}[2m])'
    )
    starfish_core_busy = by_node(
        'rate(core_lock_util{node=~"node-[0-9]+"}[2m])'
    )
    core_busy = first_present(
        f"{vantage_core_busy} * 100 / 1e6",
        f"{starfish_core_busy} * 100 / 1e6",
    )
    core_queue = first_present(
        by_node('core_queue_length{node=~"node-[0-9]+-primary"}'),
        by_node('core_queue_length{node=~"node-[0-9]+"}'),
    )

    per_node_cpu = by_node("rate(process_cpu_seconds_total[2m])*100")
    per_node_memory = by_node("process_resident_memory_bytes")
    per_node_bytes = by_node("rate(bytes_sent_total[2m])")
    median_bandwidth = f'quantile(0.5, {per_node_bytes}) or vector(0)'

    panels = [
        stat("Validators (live)", "short", [
            (live_validators, "validators"),
        ], 0, w=4),
        stat("Protocol", "short", [
            ('max by (protocol) (protocol_info == 1)', "{{protocol}}"),
            ('max by (protocol) (consensus_protocol_info == 1)', "{{protocol}}"),
        ], 4, text_mode="name", w=5),
        stat("Tx mode", "short", [
            ('max by (mode) (transaction_mode_info{mode!=""} == 1)', "{{mode}}"),
            ('max by (mode) (label_replace(transaction_mode_info{mode=""} == 1, '
             '"mode", "random", "job", ".*"))', "{{mode}}"),
            ('max by (mode) (label_replace(transaction_mode_info{mode=""} == 0, '
             '"mode", "all-zero", "job", ".*"))', "{{mode}}"),
        ], 9, text_mode="name", w=5),
        stat("Committed TPS", "cps", [
            (median_tps, "median validator"),
        ], 14, w=5),
        stat("Bandwidth out p50 (per validator)", "Bps", [
            (median_bandwidth, "median validator"),
        ], 19, w=5),
        ts("Committed throughput per validator", "cps", bands(per_node_tps), 3),
        ts("Transaction latency, median across nodes (ms)", "ms", [
            ('quantile(0.5, transaction_materialised_latency{v="p50",node=~"node-[0-9]+-worker-0"})/1000', "materialised p50"),
            ('quantile(0.5, transaction_materialised_latency{v="p99",node=~"node-[0-9]+-worker-0"})/1000', "materialised p99"),
            ('quantile(0.5, transaction_committed_latency{v="p50",node=~"node-[0-9]+-worker-0"})/1000', "committed p50"),
            ('quantile(0.5, transaction_committed_latency{v="p99",node=~"node-[0-9]+-worker-0"})/1000', "committed p99"),
            ('quantile(0.5, block_committed_latency{v="p50"})/1000', "block p50"),
            ('quantile(0.5, block_committed_latency{v="p99"})/1000', "block p99"),
            ('quantile(0.5, transaction_committed_latency{v="p50",node=~"node-[0-9]+"})/1000', "committed p50"),
        ], 11),
        ts("Consensus progress (view or round)", "short", bands(progress),
           19, x=0, w=12),
        ts("Consensus progress rate", "cps", bands(progress_rate),
           19, x=12, w=12),
        ts("Core busy", "percent", bands(core_busy), 27, x=0, w=12),
        ts("Core queue depth", "short", bands(core_queue), 27, x=12, w=12),
        ts("CPU per validator", "percent", bands(per_node_cpu), 35, x=0, w=12),
        ts("Memory per validator", "bytes", bands(per_node_memory), 35, x=12, w=12),
        ts("Network out per validator", "Bps", bands(per_node_bytes), 43),
    ]
    dash = {"uid": "wanbench", "title": "wan-bench", "schemaVersion": 39,
            "time": {"from": "now-15m", "to": "now"}, "refresh": "5s", "panels": panels}
    return json.dumps(dash)


def _normalize_datasource(dashboard_json: str) -> str:
    """Point a native dashboard at the provisioned datasource."""
    return re.sub(
        r'"(?:Fixed-UID-[A-Za-z0-9_-]+|\$\{DS_PROMETHEUS\})"',
        '"prom"',
        dashboard_json,
    )


def _portable_dashboard(dashboard_json: str, started_at: str | None,
                        finished_at: str | None) -> str:
    """Return an importable dashboard with a selectable Prometheus source."""
    dashboard = json.loads(_normalize_datasource(dashboard_json))
    if started_at and finished_at:
        dashboard["time"] = {"from": started_at, "to": finished_at}
    inputs = [
        item for item in dashboard.get("__inputs", [])
        if item.get("name") != "DS_PROMETHEUS"
    ]
    inputs.append({
        "name": "DS_PROMETHEUS",
        "label": "Prometheus",
        "description": "Prometheus containing the wan-bench archive",
        "type": "datasource",
        "pluginId": "prometheus",
        "pluginName": "Prometheus",
    })
    dashboard["__inputs"] = inputs
    body = json.dumps(dashboard, indent=2)
    return re.sub(
        r'("uid"\s*:\s*)"prom"',
        r'\1"${DS_PROMETHEUS}"',
        body,
    ) + "\n"


def write_archive_bundle(outdir: str | pathlib.Path,
                         started_at: str | None,
                         finished_at: str | None,
                         dashboards: list[str] | None = None) -> pathlib.Path:
    """Write a portable Grafana view for an archived Prometheus snapshot."""
    root = pathlib.Path(outdir)
    bundle = root / "monitoring"
    provisioned = bundle / "provisioning"
    dashboard_dir = provisioned / "dashboards"
    datasource_dir = provisioned / "datasources"
    dashboard_dir.mkdir(parents=True, exist_ok=True)
    datasource_dir.mkdir(parents=True, exist_ok=True)

    generic = _dashboard_json()
    fixed = json.loads(_normalize_datasource(generic))
    if started_at and finished_at:
        fixed["time"] = {"from": started_at, "to": finished_at}
    (dashboard_dir / "wanbench.json").write_text(json.dumps(fixed, indent=2) + "\n")
    (bundle / "dashboard.json").write_text(
        _portable_dashboard(generic, started_at, finished_at)
    )

    for index, path in enumerate(dashboards or []):
        source = pathlib.Path(path)
        try:
            body = source.read_text()
            fixed_body = _normalize_datasource(body)
            portable_body = _portable_dashboard(body, started_at, finished_at)
        except (OSError, json.JSONDecodeError) as exc:
            print(f"monitoring: WARNING cannot export dashboard {path} ({exc})",
                  flush=True)
            continue
        (dashboard_dir / f"native-{index}.json").write_text(fixed_body)
        (bundle / f"dashboard-native-{index}.json").write_text(
            portable_body
        )

    (datasource_dir / "prometheus.yml").write_text(
        _datasource_yml().replace(
            f"http://localhost:{PROM_PORT}", f"http://prometheus:{PROM_PORT}"
        )
    )
    (dashboard_dir / "provider.yml").write_text(_dashboards_provider_yml())
    (bundle / "compose.yaml").write_text(_archive_compose_yml())
    (bundle / "README.md").write_text(_archive_readme())
    print(f"monitoring: portable Grafana bundle -> {bundle}", flush=True)
    return bundle


def _archive_compose_yml() -> str:
    return """services:
  unpack:
    image: alpine
    command:
      - sh
      - -ec
      - >-
        find /data -mindepth 1 -maxdepth 1 -exec rm -rf {} +;
        tar -xzf /bundle/prometheus-tsdb.tar.gz --strip-components=1 -C /data;
        chmod -R a+rwX /data
    volumes:
      - ../prometheus-tsdb.tar.gz:/bundle/prometheus-tsdb.tar.gz:ro
      - prometheus-data:/data
  prometheus:
    image: prom/prometheus
    depends_on:
      unpack:
        condition: service_completed_successfully
    command:
      - --config.file=/etc/prometheus/prometheus.yml
      - --storage.tsdb.path=/prometheus
      - --storage.tsdb.retention.time=10y
    ports:
      - "${WANBENCH_PROMETHEUS_PORT:-9090}:9090"
    volumes:
      - prometheus-data:/prometheus
  grafana:
    image: grafana/grafana
    depends_on:
      - prometheus
    environment:
      GF_AUTH_ANONYMOUS_ENABLED: "true"
      GF_AUTH_ANONYMOUS_ORG_ROLE: Viewer
      GF_AUTH_DISABLE_LOGIN_FORM: "true"
      GF_PLUGINS_PREINSTALL_DISABLED: "true"
    ports:
      - "${WANBENCH_GRAFANA_PORT:-3000}:3000"
    volumes:
      - ./provisioning:/etc/grafana/provisioning:ro
volumes:
  prometheus-data:
"""


def _archive_readme() -> str:
    return """# Archived metrics

Start the included Prometheus and Grafana:

```bash
docker compose -f monitoring/compose.yaml up
```

Open <http://localhost:3000/d/wanbench/wan-bench>. Stop it with
`docker compose -f monitoring/compose.yaml down -v`.

For an existing Grafana, start only `prometheus`, add it as a Prometheus
datasource, then import `monitoring/dashboard.json` and select that datasource.
The local endpoint is <http://localhost:9090>. Set
`WANBENCH_PROMETHEUS_PORT` or `WANBENCH_GRAFANA_PORT` to avoid port conflicts.
"""


def start(aws: Aws, ssh: Ssh, cfg: RunConfig, control: Host, nodes: list[Host],
          targets: list[str | tuple[str, dict]],
          dashboards: list[str] | None = None) -> str:
    """Start monitoring and return the Grafana URL."""
    # Empty CIDR disables external Grafana access; None detects this host's /32.
    if cfg.grafana_open_cidr == "":
        cidr = None
        aws.close_port(GRAFANA_PORT)
        print("monitoring: grafana_open_cidr is \"\" -- Grafana port stays closed",
              flush=True)
    else:
        cidr = cfg.grafana_open_cidr or _my_ip()
        aws.open_port(GRAFANA_PORT, cidr)

    prov = "/opt/mon/grafana/provisioning"
    ssh.sudo(
        control,
        f"mkdir -p {prov}/datasources {prov}/dashboards /opt/mon/prometheus-data; "
        "chmod 0777 /opt/mon/prometheus-data",
    )
    ssh.run(control, f"echo {_q(_prometheus_yml(targets, cfg.prometheus_scrape_interval_s))} "
                     "| sudo tee /opt/mon/prometheus.yml >/dev/null")
    ssh.run(control, f"echo {_q(_datasource_yml())} | sudo tee {prov}/datasources/ds.yml >/dev/null")
    ssh.run(control, f"echo {_q(_dashboards_provider_yml())} | sudo tee {prov}/dashboards/provider.yml >/dev/null")
    # Copy dashboard files instead of passing large JSON through the shell.
    def ship_dashboard(name, body):
        with tempfile.NamedTemporaryFile("w", suffix=".json") as tf:
            tf.write(body)
            tf.flush()
            ssh.scp(control, tf.name, f"/tmp/{name}")
        ssh.sudo(control, f"mv /tmp/{name} {prov}/dashboards/{name}")

    ship_dashboard("wanbench.json", _dashboard_json())
    for i, path in enumerate(dashboards or []):
        try:
            body = _normalize_datasource(pathlib.Path(path).read_text())
        except OSError:
            print(f"monitoring: dashboard {path} not found, skipping", flush=True)
            continue
        ship_dashboard(f"native-{i}.json", body)
    if dashboards:
        print(f"monitoring: provisioned {len(dashboards)} native dashboard(s)", flush=True)
    # Grafana runs as uid 472 and must read the copied files.
    ssh.sudo(control, f"chmod -R a+rX {prov}")

    # Anonymous access is read-only and restricted by the security-group CIDR.
    ssh.sudo(control,
             "docker rm -f mon-prom mon-graf 2>/dev/null || true; "
             f"docker run -d --restart always --name mon-prom --network host "
             "-v /opt/mon/prometheus.yml:/etc/prometheus/prometheus.yml "
             "-v /opt/mon/prometheus-data:/prometheus prom/prometheus "
             "--config.file=/etc/prometheus/prometheus.yml "
             "--storage.tsdb.path=/prometheus --storage.tsdb.retention.time=2h "
             "--web.enable-admin-api; "
             f"docker run -d --restart always --name mon-graf --network host "
             "-e GF_AUTH_ANONYMOUS_ENABLED=true -e GF_AUTH_ANONYMOUS_ORG_ROLE=Viewer "
             "-e GF_AUTH_DISABLE_LOGIN_FORM=true -e GF_AUTH_BASIC_ENABLED=false "
             f"-v {prov}:/etc/grafana/provisioning grafana/grafana")

    url = f"http://{control.public_ip}:{GRAFANA_PORT}/d/wanbench/wan-bench"
    scope = f"open to {cidr}" if cidr else "not exposed"
    print(f"monitoring: Grafana at {url} ({scope}, no login); "
          f"Prometheus scraping {len(nodes)} validators every "
          f"{cfg.prometheus_scrape_interval_s}s", flush=True)
    return url


def archive_prometheus(ssh: Ssh, control: Host,
                       outdir: str | pathlib.Path) -> pathlib.Path:
    """Snapshot the live Prometheus database and copy it locally."""
    response = ssh.run(
        control,
        f"curl -fsS -XPOST http://127.0.0.1:{PROM_PORT}/api/v1/admin/tsdb/snapshot",
        timeout=120,
    )
    payload = json.loads(response)
    name = payload.get("data", {}).get("name") if payload.get("status") == "success" else None
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9._-]+", name):
        raise RuntimeError(f"invalid Prometheus snapshot response: {response[:500]}")

    remote = "/tmp/wanbench-prometheus-tsdb.tar.gz"
    ssh.sudo(
        control,
        f"tar -C /opt/mon/prometheus-data/snapshots -czf {remote} {name}; "
        f"chmod a+r {remote}",
        timeout=600,
    )
    out = pathlib.Path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    target = out / "prometheus-tsdb.tar.gz"
    partial = target.with_suffix(target.suffix + ".tmp")
    ssh.fetch(control, remote, str(partial), timeout=600)
    partial.replace(target)
    print(f"monitoring: archived Prometheus TSDB ({target.stat().st_size} bytes) -> {target}",
          flush=True)
    return target


def configure_targets(ssh: Ssh, cfg: RunConfig, control: Host,
                      targets: list[tuple[str, dict]]) -> None:
    """Replace Prometheus targets for the next campaign variant."""
    content = _prometheus_yml(targets, cfg.prometheus_scrape_interval_s)
    ssh.run(
        control,
        f"echo {_q(content)} | sudo tee /opt/mon/prometheus.yml >/dev/null && "
        "sudo docker kill --signal=HUP mon-prom >/dev/null",
    )


def _q(s: str) -> str:
    import shlex
    return shlex.quote(s)

# Archived metrics

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

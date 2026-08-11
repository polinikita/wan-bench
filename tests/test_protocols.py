import json
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from wanbench.cli import _cfg
from wanbench.config import RunConfig, WanConfig
from wanbench.deploy import (CLIENT_ACTIVATION_MARGIN_MS, _deploy_starfish,
                             deploy, launch_nodes)
from wanbench.monitoring import (_dashboard_json, _portable_dashboard,
                                 _prometheus_yml, validator_targets,
                                 write_archive_bundle)
from wanbench.protocols import Starfish, Vantage, _docker_prefix
from wanbench.ssh import Host

class ProtocolTests(unittest.TestCase):
    def test_prometheus_scrape_interval_is_configurable_for_onset_debugging(self):
        yml = _prometheus_yml(["10.0.0.1:6003"], scrape_interval_s=5)
        self.assertIn("scrape_interval: 5s", yml)
        self.assertIn("scrape_timeout: 5s", yml)
        self.assertIn("evaluation_interval: 5s", yml)

    def test_prometheus_scrape_interval_must_be_positive(self):
        cfg = RunConfig(nodes=4, rate=400, image="image", key_name="key",
                        prometheus_scrape_interval_s=0)
        with self.assertRaisesRegex(ValueError, "prometheus_scrape_interval_s"):
            cfg.validate()

    def test_validator_targets_include_archive_labels(self):
        cfg = RunConfig(protocol="vantage", nodes=1, rate=100, image="image")
        hosts = [Host(0, "i-0", "public", "10.0.0.1")]
        targets = validator_targets(
            cfg, hosts, {"wanbench_variant": "vantage", "wanbench_rate": "100"})
        self.assertEqual(len(targets), 2)
        for _target, labels in targets:
            self.assertEqual(labels["wanbench_variant"], "vantage")
            self.assertEqual(labels["wanbench_rate"], "100")
            self.assertIn("node", labels)

    def test_external_access_cidrs_must_be_ipv4_networks(self):
        for field, value in (
            ("ssh_open_cidr", ""),
            ("ssh_open_cidr", "2001:db8::/64"),
            ("grafana_open_cidr", "not-a-cidr"),
        ):
            cfg = RunConfig(nodes=4, rate=400, image="image", key_name="key",
                            **{field: value})
            with self.subTest(field=field, value=value), \
                 self.assertRaisesRegex(ValueError, field):
                cfg.validate()

    def test_grafana_anonymous_access_is_read_only(self):
        import wanbench.monitoring as monitoring
        cfg = RunConfig(nodes=4, rate=400, image="image", grafana_open_cidr="")
        control = Host(4, "i-control", "public-control", "10.0.0.5")
        ssh = MagicMock()
        monitoring.start(MagicMock(), ssh, cfg, control, [], [])
        launch = ssh.sudo.call_args_list[-1].args[1]
        self.assertIn("GF_AUTH_ANONYMOUS_ORG_ROLE=Viewer", launch)
        self.assertNotIn("GF_AUTH_ANONYMOUS_ORG_ROLE=Admin", launch)
        self.assertIn("/opt/mon/prometheus-data:/prometheus", launch)
        self.assertIn("--web.enable-admin-api", launch)

    def test_prometheus_snapshot_is_copied_atomically(self):
        import wanbench.monitoring as monitoring
        control = Host(4, "i-control", "public-control", "10.0.0.5")
        ssh = MagicMock()
        ssh.run.return_value = json.dumps({
            "status": "success",
            "data": {"name": "20260811T000000Z-test"},
        })

        def fetch(_host, _remote, local, timeout):
            self.assertEqual(timeout, 600)
            Path(local).write_bytes(b"snapshot")

        ssh.fetch.side_effect = fetch
        with tempfile.TemporaryDirectory() as tmp:
            path = monitoring.archive_prometheus(ssh, control, tmp)
            self.assertEqual(path.read_bytes(), b"snapshot")
            self.assertFalse(path.with_suffix(".gz.tmp").exists())
        self.assertIn("/api/v1/admin/tsdb/snapshot", ssh.run.call_args.args[1])
        self.assertIn("20260811T000000Z-test", ssh.sudo.call_args.args[1])

    def test_archive_dashboard_selects_the_callers_datasource(self):
        body = _portable_dashboard(
            _dashboard_json(), "2026-08-11T12:00:00+00:00",
            "2026-08-11T13:00:00+00:00")
        dashboard = json.loads(body)
        self.assertEqual(dashboard["__inputs"][-1]["name"], "DS_PROMETHEUS")
        self.assertEqual(
            dashboard["time"],
            {"from": "2026-08-11T12:00:00+00:00",
             "to": "2026-08-11T13:00:00+00:00"},
        )
        self.assertTrue(all(
            panel["datasource"]["uid"] == "${DS_PROMETHEUS}"
            for panel in dashboard["panels"]
        ))

    def test_archive_bundle_contains_compose_and_import_dashboard(self):
        with tempfile.TemporaryDirectory() as tmp:
            bundle = write_archive_bundle(
                tmp, "2026-08-11T12:00:00+00:00",
                "2026-08-11T13:00:00+00:00")
            self.assertTrue((bundle / "compose.yaml").is_file())
            self.assertTrue((bundle / "dashboard.json").is_file())
            self.assertTrue((bundle / "README.md").is_file())
            compose = (bundle / "compose.yaml").read_text()
            self.assertIn("--config.file=/etc/prometheus/prometheus.yml", compose)
            datasource = (bundle / "provisioning" / "datasources" /
                          "prometheus.yml").read_text()
            self.assertIn("http://prometheus:9090", datasource)

    def test_docker_prefix_rejects_inexact_aggregate_rate(self):
        cfg = RunConfig(nodes=50, rate=51, image="image")
        with self.assertRaisesRegex(ValueError, "divisible"):
            _docker_prefix(cfg, 0, "entrypoint")

    def test_node_container_raises_the_file_descriptor_limit(self):
        cfg = RunConfig(nodes=100, rate=1000, image="image")
        cmd = _docker_prefix(cfg, 0, "entrypoint")
        self.assertIn("--ulimit nofile=65536:65536", cmd)

    def test_spam_delay_budget_scales_with_committee_size(self):
        for nodes, expected_ms in ((4, 10_800), (10, 12_000), (20, 14_000),
                                   (50, 20_000), (100, 30_000)):
            cfg = RunConfig(nodes=nodes, rate=nodes * 100, image="image")
            self.assertEqual(cfg.spam_delay_budget_ms(), expected_ms, f"n={nodes}")

    def test_spam_delay_budget_stays_under_the_default_warmup(self):
        for nodes in (4, 10, 20, 50):
            cfg = RunConfig(nodes=nodes, rate=nodes * 100, image="image")
            self.assertLess(cfg.spam_delay_budget_ms(), 30_000, f"n={nodes}")

    def test_validator_launch_uses_full_committee_concurrency(self):
        hosts = [Host(i, f"i-{i}", f"pub-{i}", f"10.0.0.{i + 1}") for i in range(100)]
        cfg = RunConfig(nodes=100, rate=1_000, image="image")
        ssh = MagicMock()

        launch_nodes(ssh, cfg, hosts, hosts)

        self.assertEqual(ssh.fanout.call_args.kwargs["max_workers"], 100)

    def test_client_submits_ahead_of_the_nodes_gate_by_the_lead(self):
        hosts = [Host(i, f"i-{i}", f"pub-{i}", f"10.0.0.{i + 1}") for i in range(4)]
        cfg = RunConfig(nodes=4, rate=400, image="image")
        cfg.client_activate_at_ms = 1_770_000_000_000
        cfg.metrics_active_at_ms = cfg.client_activate_at_ms + cfg.spam_lead_ms
        adapter = Vantage(cfg)
        self.assertEqual(adapter.parameters()["metrics_active_at_ms"],
                         1_770_000_000_000 + cfg.spam_lead_ms)
        self.assertIn("ACTIVATE_AT_MS=1770000000000", adapter.run_cmd(hosts[0], hosts))
        self.assertNotIn(f"ACTIVATE_AT_MS={cfg.metrics_active_at_ms}",
                         adapter.run_cmd(hosts[0], hosts))

    def test_deploy_derives_both_instants_from_one_anchor(self):
        hosts = [Host(i, f"i-{i}", f"pub-{i}", f"10.0.0.{i + 1}") for i in range(100)]
        cfg = RunConfig(nodes=100, rate=1_000, image="image")
        control = Host(100, "i-control", "control", "10.0.1.1")
        ssh = MagicMock()
        ssh.run.return_value = '{"name": "private-key"}'
        ssh.parallel.side_effect = lambda items, action: [action(host) for host in items]
        pubkeys = [{"name": f"key-{i}"} for i in range(100)]

        with patch("wanbench.deploy.time.time", return_value=1_000):
            deploy(ssh, cfg, control, hosts, pubkeys=pubkeys)

        self.assertEqual(cfg.spam_delay_budget_ms(), 30_000)
        self.assertEqual(
            cfg.client_activate_at_ms,
            1_000_000 + CLIENT_ACTIVATION_MARGIN_MS + 30_000,
        )
        self.assertEqual(cfg.metrics_active_at_ms - cfg.client_activate_at_ms,
                         cfg.spam_lead_ms)
        launch = ssh.fanout.call_args.args[1]
        self.assertIn(
            f"ACTIVATE_AT_MS={cfg.client_activate_at_ms}",
            launch(hosts[0]),
        )

    def test_zero_lead_restores_the_single_instant_behaviour(self):
        cfg = RunConfig(nodes=4, rate=400, image="image", spam_lead_ms=0)
        cfg.client_activate_at_ms = 1_770_000_000_000
        cfg.metrics_active_at_ms = cfg.client_activate_at_ms + cfg.spam_lead_ms
        self.assertEqual(cfg.metrics_active_at_ms, cfg.client_activate_at_ms)

    def test_no_metrics_active_window_omits_the_client_flag(self):
        hosts = [Host(i, f"i-{i}", f"pub-{i}", f"10.0.0.{i + 1}") for i in range(4)]
        cfg = RunConfig(nodes=4, rate=400, image="image")
        adapter = Vantage(cfg)
        self.assertIsNone(adapter.parameters()["metrics_active_at_ms"])
        self.assertNotIn("ACTIVATE_AT_MS", adapter.run_cmd(hosts[0], hosts))

    def test_vantage_state_sync_policy_is_written_to_parameters(self):
        cfg = RunConfig(nodes=4, rate=400, image="image",
                        sequence_checkpoint_interval_views=20,
                        sequence_sync_min_gap_views=50,
                        sequence_sync_chunk_outcomes=64,
                        sequence_sync_chunk_outcome_items=800)
        parameters = Vantage(cfg).parameters()
        self.assertTrue(parameters["sequence_checkpoints"])
        self.assertTrue(parameters["sequence_install_enabled"])
        self.assertEqual(parameters["sequence_checkpoint_interval_views"], 20)
        self.assertEqual(parameters["sequence_sync_min_gap_views"], 50)
        self.assertEqual(parameters["sequence_sync_chunk_outcomes"], 64)
        self.assertEqual(parameters["sequence_sync_chunk_outcome_items"], 800)

    def test_vantage_uses_echo_availability_claims_by_default(self):
        cfg = RunConfig(nodes=4, rate=400, image="image")
        self.assertTrue(Vantage(cfg).parameters()["echo_avail_claims"])

        cfg.echo_avail_claims = False
        self.assertFalse(Vantage(cfg).parameters()["echo_avail_claims"])

    def test_vantage_uses_100ms_header_delay_by_default(self):
        cfg = RunConfig(nodes=4, rate=400, image="image")
        self.assertEqual(Vantage(cfg).parameters()["max_header_delay"], 100)

    def test_vantage_data_plane_uses_only_private_addresses(self):
        hosts = [
            Host(i, f"i-{i}", f"198.51.100.{i + 1}", f"10.0.0.{i + 1}")
            for i in range(4)
        ]
        cfg = RunConfig(nodes=4, rate=400, image="image")
        adapter = Vantage(cfg)

        committee = json.dumps(adapter.committee(
            hosts, [{"name": f"key-{i}"} for i in range(4)]
        ))
        command = adapter.run_cmd(hosts[0], hosts)
        for host in hosts:
            self.assertIn(host.private_ip, committee)
            self.assertIn(host.private_ip, command)
            self.assertNotIn(host.public_ip, committee)
            self.assertNotIn(host.public_ip, command)

    def test_vantage_state_sync_can_be_disabled_in_parameters(self):
        cfg = RunConfig(nodes=4, rate=400, image="image",
                        sequence_checkpoints=False,
                        sequence_install_enabled=False)
        parameters = Vantage(cfg).parameters()
        self.assertFalse(parameters["sequence_checkpoints"])
        self.assertFalse(parameters["sequence_install_enabled"])

    def test_no_state_sync_cli_override_disables_both_config_bits(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "run.yaml"
            path.write_text(
                "nodes: 4\n"
                "rate: 400\n"
                "image: image\n"
                "key_name: key\n"
            )
            cfg = _cfg(SimpleNamespace(config=str(path), no_state_sync=True))

        self.assertFalse(cfg.sequence_checkpoints)
        self.assertFalse(cfg.sequence_install_enabled)

    def test_vantage_state_sync_policy_rejects_invalid_bounds(self):
        for field, value in (
            ("sequence_checkpoint_interval_views", 0),
            ("sequence_sync_min_gap_views", -1),
            ("sequence_sync_chunk_outcomes", 0),
            ("sequence_sync_chunk_outcome_items", 0),
        ):
            cfg = RunConfig(nodes=4, rate=400, image="image", key_name="key",
                            **{field: value})
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, field):
                cfg.validate()

    def test_config_rejects_unrepresentable_mimic_settings(self):
        cfg = RunConfig(nodes=4, rate=100, image="image", key_name="key",
                        wan=WanConfig(mode="mimic", jitter_ms=1))
        with self.assertRaisesRegex(ValueError, "fixed RTT/2"):
            cfg.validate()

    def test_config_rejects_too_small_starfish_committee(self):
        cfg = RunConfig(protocol="starfish", nodes=3, rate=99, image="image",
                        key_name="key")
        with self.assertRaisesRegex(ValueError, "at least 4"):
            cfg.validate()

    def test_config_rejects_silently_ignored_starfish_consensus(self):
        def cfg(flags):
            return RunConfig(protocol="starfish", nodes=4, rate=100, image="image",
                             key_name="key", protocol_flags=flags)

        with self.assertRaisesRegex(ValueError, "requires an explicit"):
            cfg([]).validate()
        with self.assertRaisesRegex(ValueError, "unknown starfish consensus"):
            cfg(["--consensus", "bluestrek"]).validate()
        with self.assertRaisesRegex(ValueError, "has no value"):
            cfg(["--consensus"]).validate()
        for name in ("bluestreak", "sailfish-pp", "sailfish++", "starfish"):
            cfg(["--consensus", name]).validate()  # must not raise

    def test_starfish_command_matches_generated_config_files(self):
        cfg = RunConfig(protocol="starfish", nodes=4, rate=100, image="image")
        hosts = [Host(i, f"i-{i}", "public", f"10.0.0.{i + 1}") for i in range(4)]
        cmd = Starfish(cfg).run_cmd(hosts[2], hosts)
        self.assertIn("--committee-path /wanbench/committee.yaml", cmd)
        self.assertIn("--public-config-path /wanbench/public-config.yaml", cmd)
        self.assertIn("--private-config-path /wanbench/private-config-2.yaml", cmd)
        self.assertIn("--parameters-path /wanbench/parameters.yaml", cmd)

    def test_dashboard_excludes_vantage_primary_latency(self):
        dashboard = _dashboard_json()
        self.assertIn('node-[0-9]+-worker-0', dashboard)
        self.assertNotIn('latency_s', dashboard)
        self.assertEqual(json.loads(dashboard)["refresh"], "5s")

    def test_dashboard_legends_and_titles_never_name_a_protocol_or_codebase(self):
        panels = json.loads(_dashboard_json())["panels"]
        leaked = []
        for panel in panels:
            for name in ("vantage", "starfish", "bluestreak"):
                if name in panel.get("title", "").lower():
                    leaked.append(f"panel title {panel['title']!r}")
            for target in panel.get("targets", []):
                legend = target.get("legendFormat", "")
                for name in ("vantage", "starfish", "bluestreak"):
                    if name in legend.lower():
                        leaked.append(f"legendFormat {legend!r}")
        self.assertEqual(leaked, [])

    def test_dashboard_validator_series_are_bounded(self):
        panels = json.loads(_dashboard_json())["panels"]
        time_series = [panel for panel in panels if panel["type"] == "timeseries"]
        self.assertTrue(all(
            target["legendFormat"] != "{{n}}"
            for panel in time_series
            for target in panel["targets"]
        ))
        throughput = next(
            panel for panel in panels
            if panel["title"] == "Committed throughput per validator"
        )
        self.assertEqual(
            [target["legendFormat"] for target in throughput["targets"]],
            ["min", "p50", "max"],
        )

    def test_dashboard_shared_progress_and_core_panels_support_both_codebases(self):
        panels = {
            panel["title"]: " ".join(target["expr"] for target in panel["targets"])
            for panel in json.loads(_dashboard_json())["panels"]
        }
        self.assertIn("vantage_entered_view",
                      panels["Consensus progress (view or round)"])
        self.assertIn("dag_highest_round",
                      panels["Consensus progress (view or round)"])
        self.assertIn("utilization_timer", panels["Core busy"])
        self.assertIn("core_lock_util", panels["Core busy"])
        self.assertIn("core_queue_length", panels["Core queue depth"])

    def test_dashboard_latency_panel_legends_are_metric_semantic(self):
        panels = json.loads(_dashboard_json())["panels"]
        latency_panel = next(
            p for p in panels
            if p["title"] == "Transaction latency, median across nodes (ms)"
        )
        legends = [t["legendFormat"] for t in latency_panel["targets"]]
        self.assertEqual(
            legends,
            [
                "materialised p50", "materialised p99",
                "committed p50", "committed p99",
                "block p50", "block p99",
                "committed p50",
            ],
        )
        exprs = [t["expr"] for t in latency_panel["targets"]]
        committed_exprs = [e for e, lg in zip(exprs, legends) if lg == "committed p50"]
        self.assertEqual(len(committed_exprs), 2)
        self.assertIn('node-[0-9]+-worker-0', committed_exprs[0])
        self.assertIn('node=~"node-[0-9]+"', committed_exprs[1])
        self.assertFalse(re.fullmatch(r'node-[0-9]+', 'node-3-worker-0'))

    def test_dashboard_top_summary_supports_both_codebases(self):
        panels = json.loads(_dashboard_json())["panels"]
        summary = [p for p in panels if p["type"] == "stat"]
        self.assertEqual(
            [panel["title"] for panel in summary],
            ["Validators (live)", "Protocol", "Tx mode", "Committed TPS",
             "Bandwidth out p50 (per validator)"],
        )
        self.assertTrue(
            all(target["instant"] for panel in summary for target in panel["targets"])
        )
        spanned = 0
        for panel in summary:
            self.assertEqual(panel["gridPos"]["x"], spanned,
                             f"{panel['title']} leaves a gap at x={spanned}")
            self.assertEqual(panel["gridPos"]["y"], 0)
            spanned += panel["gridPos"]["w"]
        self.assertEqual(spanned, 24, "summary row must span the grid exactly")

        expressions = {
            panel["title"]: " ".join(target["expr"] for target in panel["targets"])
            for panel in summary
        }
        self.assertIn("up{", expressions["Validators (live)"])
        self.assertIn("protocol_info", expressions["Protocol"])
        self.assertIn("consensus_protocol_info", expressions["Protocol"])
        self.assertIn('mode!=""', expressions["Tx mode"])
        self.assertIn('mode=""', expressions["Tx mode"])
        self.assertIn("committed_transactions", expressions["Committed TPS"])
        self.assertIn("sequenced_transactions_total", expressions["Committed TPS"])
        bandwidth = expressions["Bandwidth out p50 (per validator)"]
        self.assertIn("bytes_sent_total", bandwidth)
        self.assertIn("quantile(0.5", bandwidth)

        for i, left in enumerate(panels):
            a = left["gridPos"]
            for right in panels[i + 1:]:
                b = right["gridPos"]
                overlaps = (
                    a["x"] < b["x"] + b["w"]
                    and b["x"] < a["x"] + a["w"]
                    and a["y"] < b["y"] + b["h"]
                    and b["y"] < a["y"] + a["h"]
                )
                self.assertFalse(overlaps, f"{left['title']} overlaps {right['title']}")

    def test_starfish_deploy_distributes_current_cli_artifacts(self):
        cfg = RunConfig(protocol="starfish", nodes=4, rate=100, image="image")
        hosts = [Host(i, f"i-{i}", "public", f"10.0.0.{i + 1}") for i in range(4)]
        control = Host(-1, "i-control", "public", "10.0.0.10")
        ssh = MagicMock()

        def run(_host, command, **_kwargs):
            if "cat /opt/wanbench/" in command:
                return f"contents of {Path(command).name}"
            return ""

        def fanout(items, command, **_kwargs):
            output = []
            for host in items:
                remote = command(host) if callable(command) else command
                output.append(run(host, remote) if remote else "")
            return output

        ssh.run.side_effect = run
        ssh.fanout.side_effect = fanout
        ssh.parallel.side_effect = lambda items, action: [action(host) for host in items]
        _deploy_starfish(ssh, cfg, control, hosts)

        commands = [call.args[1] for call in ssh.run.call_args_list]
        self.assertTrue(any("benchmark-genesis" in command and
                            "--node-parameters-path" in command
                            for command in commands))
        remotes = [call.args[2] for call in ssh.scp.call_args_list]
        self.assertIn("/opt/wanbench/committee.yaml", remotes)
        self.assertIn("/opt/wanbench/public-config.yaml", remotes)
        self.assertIn("/opt/wanbench/private-config-3.yaml", remotes)
        self.assertIn("/opt/wanbench/parameters.yaml", remotes)

if __name__ == "__main__":
    unittest.main()

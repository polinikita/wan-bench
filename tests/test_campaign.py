import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import yaml

from wanbench import campaign as campaign_mod
from wanbench import matrix as matrix_mod
from wanbench.campaign import CampaignConfig
from wanbench.ssh import Host

def campaign_yaml(key_path: str) -> str:
    return f"""
name: test-campaign
output: results/test-campaign
rates: [100, 200]
warmup_s: 0
window_s: 1
base:
  region: eu-north-1
  instance_type: c5d.2xlarge
  key_name: key
  ssh_key_path: {key_path}
  nodes: 4
  tx_size: 512
  fault:
    kind: none
variants:
  - name: vantage
    protocol: vantage
    image: registry/vantage:latest
    metrics_port: 6003
  - name: bluestreak
    protocol: starfish
    image: registry/starfish:latest
    metrics_port: 1500
    protocol_flags: [--consensus, bluestreak]
"""

class CampaignConfigTests(unittest.TestCase):
    def load(self, root: str) -> CampaignConfig:
        path = Path(root) / "campaign.yaml"
        path.write_text(campaign_yaml(str(Path(root) / "key")))
        return CampaignConfig.load(str(path))

    def test_one_manifest_builds_all_protocol_configs(self):
        with tempfile.TemporaryDirectory() as tmp:
            campaign = self.load(tmp)
        configs = campaign.configs()
        self.assertEqual([name for name, _cfg in configs], ["vantage", "bluestreak"])
        self.assertEqual([cfg.protocol for _name, cfg in configs], ["vantage", "starfish"])
        self.assertTrue(all(cfg.run_id == "test-campaign" for _name, cfg in configs))

    def test_variant_cannot_override_infrastructure(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "campaign.yaml"
            text = campaign_yaml(str(Path(tmp) / "key"))
            path.write_text(text.replace(
                "  - name: vantage\n",
                "  - name: vantage\n    nodes: 100\n",
            ))
            with self.assertRaisesRegex(ValueError, "unsupported fields.*nodes"):
                CampaignConfig.load(str(path))

    def test_committee_matrix_builds_independent_campaigns(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "campaign.yaml"
            manifest = yaml.safe_load(campaign_yaml(str(Path(tmp) / "key")))
            del manifest["base"]["nodes"]
            manifest["committee_sizes"] = [4, 8]
            manifest["rates"] = [200]
            path.write_text(yaml.safe_dump(manifest, sort_keys=False))
            campaign = CampaignConfig.load(str(path))
        small = campaign.for_committee(4)
        large = campaign.for_committee(8)
        self.assertEqual(campaign.committee_sizes, [4, 8])
        self.assertEqual(small.configs()[0][1].nodes, 4)
        self.assertEqual(large.configs()[0][1].nodes, 8)
        self.assertEqual(small.configs()[0][1].run_id, "test-campaign-n4")
        self.assertEqual(large.configs()[0][1].run_id, "test-campaign-n8")

    def test_committee_matrix_rejects_ambiguous_base_nodes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "campaign.yaml"
            manifest = yaml.safe_load(campaign_yaml(str(Path(tmp) / "key")))
            manifest["committee_sizes"] = [4, 8]
            path.write_text(yaml.safe_dump(manifest, sort_keys=False))
            with self.assertRaisesRegex(ValueError, "base.nodes cannot be combined"):
                CampaignConfig.load(str(path))

    def test_manifest_container_types_are_strict(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "campaign.yaml"
            original = yaml.safe_load(campaign_yaml(str(Path(tmp) / "key")))
            for field, value, error in (
                ("base", [], "base must be a mapping"),
                ("variants", {}, "variants must be a list"),
                ("rates", [True], "positive integers"),
            ):
                with self.subTest(error=error):
                    manifest = dict(original)
                    manifest[field] = value
                    path.write_text(yaml.safe_dump(manifest, sort_keys=False))
                    with self.assertRaisesRegex(ValueError, error):
                        CampaignConfig.load(str(path))

    def test_saturation_policy_is_explicit(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "campaign.yaml"
            manifest = yaml.safe_load(campaign_yaml(str(Path(tmp) / "key")))
            manifest["stop_on_drop"] = False
            manifest["strict_through_rate"] = 100
            manifest["min_offered_throughput_pct"] = 95
            path.write_text(yaml.safe_dump(manifest, sort_keys=False))
            campaign = CampaignConfig.load(str(path))
        self.assertFalse(campaign.stop_on_drop)
        self.assertEqual(campaign.strict_through_rate, 100)
        self.assertEqual(campaign.min_offered_throughput_pct, 95)

    def test_preflight_pins_images_and_checks_aws_without_creating_resources(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "key").write_text("test")
            campaign = self.load(tmp)
            with patch.object(campaign_mod.images, "pin_to_digest",
                              side_effect=lambda ref: (ref.replace(":latest", "@sha256:x"),
                                                       "sha256:x")) as pin, \
                 patch.object(campaign_mod.Aws, "preflight", return_value={
                     "account": "123", "arn": "arn:test",
                     "availability_zones": ["eu-north-1a"],
                 }) as aws_check:
                configs, report = campaign_mod.preflight(campaign)
        self.assertEqual(pin.call_count, 2)
        aws_check.assert_called_once_with()
        self.assertTrue(all("@sha256:x" in cfg.image for _name, cfg in configs))
        self.assertEqual(report["instances"], 5)

    def test_matrix_preflight_checks_only_the_largest_fleet(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "key").write_text("test")
            path = Path(tmp) / "campaign.yaml"
            manifest = yaml.safe_load(campaign_yaml(str(Path(tmp) / "key")))
            del manifest["base"]["nodes"]
            manifest["committee_sizes"] = [4, 8]
            manifest["rates"] = [200]
            path.write_text(yaml.safe_dump(manifest, sort_keys=False))
            campaign = CampaignConfig.load(str(path))
            with patch.object(campaign_mod.images, "pin_to_digest",
                              side_effect=lambda ref: (ref.replace(":latest", "@sha256:x"),
                                                       "sha256:x")) as pin, \
                 patch.object(matrix_mod.Aws, "preflight", return_value={
                     "account": "123", "arn": "arn:test",
                     "availability_zones": ["eu-north-1a"],
                 }) as aws_check:
                groups, report = matrix_mod.preflight(campaign)
        self.assertEqual(pin.call_count, 2)
        aws_check.assert_called_once_with()
        self.assertEqual(groups[-1][2][0][1].nodes, 8)
        self.assertEqual(report["committee_sizes"], [4, 8])
        self.assertEqual(report["instances"], 9)


class PaperThroughputCampaignTests(unittest.TestCase):
    def test_n100_manifest_is_pinned_and_complete(self):
        path = Path(__file__).parents[1] / "configs" / "paper-n100-throughput.yaml"
        campaign = CampaignConfig.load(str(path))
        configs = campaign.configs()

        self.assertEqual(campaign.committee_sizes, [100])
        self.assertEqual(
            campaign.rates,
            [100, 10_000, 150_000, 200_000, 225_000, 250_000, 275_000],
        )
        self.assertEqual(campaign.strict_through_rate, 10_000)
        self.assertEqual(campaign.min_offered_throughput_pct, 95)
        self.assertEqual(
            [name for name, _ in configs],
            [
                "vantage",
                "autobahn-optimistic-a2a",
                "autobahn-seamless",
                "simpleit-optrbc",
                "simpleit-bracha",
                "bluestreak",
                "sailfish-pp",
            ],
        )
        for _name, cfg in configs:
            self.assertEqual(cfg.instance_type, "c5d.2xlarge")
            self.assertEqual((cfg.region, cfg.az), ("eu-west-1", "eu-west-1a"))
            self.assertEqual(cfg.wan.mode, "netem")
            self.assertTrue(cfg.use_instance_store)
            self.assertIn("@sha256:", cfg.image)
        self.assertTrue(configs[0][1].vantage_compact_ids)


class StarfishM5dGcCampaignTests(unittest.TestCase):
    def test_manifest_is_strict_and_uses_the_gc_image(self):
        path = (Path(__file__).parents[1] / "configs" /
                "paper-n100-starfish-m5d-gc.yaml")
        campaign = CampaignConfig.load(str(path))
        configs = campaign.configs()

        self.assertEqual(campaign.committee_sizes, [100])
        self.assertEqual(
            campaign.rates,
            [200_000, 225_000, 250_000, 275_000],
        )
        self.assertIsNone(campaign.strict_through_rate)
        self.assertEqual(campaign.min_offered_throughput_pct, 95)
        self.assertTrue(campaign.stop_on_drop)
        self.assertEqual(
            [name for name, _ in configs],
            ["bluestreak", "sailfish-pp"],
        )
        for _name, cfg in configs:
            self.assertEqual(cfg.instance_type, "m5d.2xlarge")
            self.assertEqual((cfg.region, cfg.az), ("eu-west-1", "eu-west-1a"))
            self.assertEqual(cfg.wan.mode, "netem")
            self.assertTrue(cfg.use_instance_store)
            self.assertEqual(
                cfg.image,
                "ghcr.io/iotaledger/starfish-node@sha256:"
                "3f7ee52dff606214e1dd979c31783f4bb4391405c2317c4b41b0728a89d04d88",
            )


class CampaignExecutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        Path(self.tmp.name, "key").write_text("test")
        self.campaign = CampaignConfig.load(str(self._config_path()))
        self.configs = self.campaign.configs()
        self.hosts = [Host(i, f"i-{i}", f"public-{i}", f"10.0.0.{i + 1}")
                      for i in range(4)]
        self.control = Host(4, "i-control", "public-control", "10.0.0.5")
        self.aws = MagicMock()
        self.aws.fleet_info.return_value = {
            "az": "eu-north-1a",
            "instance_type": "c5d.2xlarge",
            "ami": "ami-test",
        }
        self.ssh = MagicMock()

    def tearDown(self):
        self.tmp.cleanup()

    def _config_path(self) -> Path:
        path = Path(self.tmp.name) / "campaign.yaml"
        path.write_text(campaign_yaml(str(Path(self.tmp.name) / "key")))
        return path

    def patches(self, sweep_side_effect=None):
        return (
            patch.object(campaign_mod, "up",
                         return_value=(self.aws, self.ssh, self.hosts, self.control)),
            patch.object(campaign_mod, "down"),
            patch.object(campaign_mod, "_reset_protocol_state"),
            patch.object(campaign_mod.monitoring, "configure_targets"),
            patch.object(campaign_mod.sweep_mod, "sweep",
                         side_effect=sweep_side_effect),
            patch.object(campaign_mod.images, "ensure_image",
                         side_effect=lambda _ssh, cfg, _control, _hosts: cfg.image),
            patch.object(campaign_mod.prepare, "pull_image"),
            patch.object(campaign_mod.faults, "clear"),
            patch.object(campaign_mod.prepare, "clear_wan"),
            patch.object(campaign_mod.monitoring, "archive_prometheus",
                         return_value=Path("prometheus-tsdb.tar.gz")),
        )

    def test_variants_share_one_fleet_and_teardown_once(self):
        patches = self.patches()
        with patches[0] as up, patches[1] as down, patches[2] as reset, \
             patches[3] as configure, patches[4] as sweep, patches[5], \
             patches[6], patches[7], patches[8], patches[9] as archive:
            state = campaign_mod.execute(
                self.campaign, self.configs, str(Path(self.tmp.name) / "out"))
        up.assert_called_once()
        self.assertEqual(reset.call_count, 2)
        self.assertEqual(configure.call_count, 2)
        self.assertEqual(sweep.call_count, 2)
        for call in sweep.call_args_list:
            self.assertIs(call.kwargs["fleet"][0], self.aws)
            self.assertTrue(call.kwargs["stop_on_drop"])
            self.assertIsNone(call.kwargs["strict_through_rate"])
            self.assertIsNone(call.kwargs["min_offered_throughput_pct"])
            self.assertIn("wanbench_variant", call.kwargs["metric_labels"])
        down.assert_called_once()
        archive.assert_called_once()
        self.assertEqual(state["monitoring_archive"], "prometheus-tsdb.tar.gz")
        self.assertEqual(state["monitoring_bundle"], "monitoring")
        self.assertEqual(state["status"], "completed")
        self.assertTrue(all(v["status"] == "completed" for v in state["variants"]))

    def test_variant_failure_is_checkpointed_and_campaign_continues(self):
        patches = self.patches([RuntimeError("failed point"), None])
        out = Path(self.tmp.name) / "out"
        with patches[0], patches[1] as down, patches[2], patches[3], \
             patches[4] as sweep, \
             patches[5], patches[6], patches[7], patches[8], patches[9]:
            campaign_mod.execute(self.campaign, self.configs, str(out))
        state = json.loads((out / "campaign.json").read_text())
        self.assertEqual(state["status"], "completed_with_failures")
        self.assertEqual(state["variants"][0]["status"], "failed")
        self.assertEqual(state["variants"][1]["status"], "completed")
        self.assertEqual(sweep.call_count, 2)
        down.assert_called_once()

    def test_resume_preserves_failed_variants_and_runs_pending_variants(self):
        out = Path(self.tmp.name) / "out"
        out.mkdir()
        state = campaign_mod._new_state(self.campaign, self.configs)
        state["variants"][0]["status"] = "failed"
        state["variants"][0]["error"] = "RuntimeError: exhausted"
        campaign_mod._checkpoint(out / "campaign.json", state)
        patches = self.patches()
        with patches[0], patches[1], patches[2], patches[3], \
             patches[4] as sweep, patches[5], patches[6], patches[7], \
             patches[8], patches[9]:
            resumed = campaign_mod.execute(
                self.campaign, self.configs, str(out), resume=True)
        self.assertEqual(sweep.call_count, 1)
        self.assertEqual(resumed["variants"][0]["status"], "failed")
        self.assertEqual(resumed["variants"][1]["status"], "completed")
        self.assertEqual(resumed["status"], "completed_with_failures")

    def test_node_cleanup_failure_does_not_mask_successful_teardown(self):
        patches = self.patches()
        out = Path(self.tmp.name) / "out"
        with patches[0], patches[1] as down, patches[2], patches[3], patches[4], \
             patches[5], patches[6], \
             patch.object(campaign_mod.faults, "clear",
                          side_effect=RuntimeError("node unavailable")), \
             patches[8], patches[9]:
            state = campaign_mod.execute(self.campaign, self.configs, str(out))
        down.assert_called_once()
        self.assertEqual(state["status"], "completed")
        self.assertIsNone(state["teardown_error"])
        self.assertEqual(len(state["cleanup_warnings"]), 1)
        self.assertIn("node unavailable", state["cleanup_warnings"][0])

    def test_infrastructure_teardown_failure_is_fatal(self):
        patches = self.patches()
        out = Path(self.tmp.name) / "out"
        with patches[0], \
             patch.object(campaign_mod, "down",
                          side_effect=RuntimeError("AWS deletion failed")), \
             patches[2], patches[3], patches[4], patches[5], patches[6], \
             patches[7], patches[8], patches[9]:
            with self.assertRaisesRegex(RuntimeError, "AWS deletion failed"):
                campaign_mod.execute(self.campaign, self.configs, str(out))
        state = json.loads((out / "campaign.json").read_text())
        self.assertEqual(state["status"], "failed")
        self.assertIn("AWS deletion failed", state["teardown_error"])

    def test_resume_with_all_variants_complete_creates_no_fleet(self):
        out = Path(self.tmp.name) / "out"
        state = campaign_mod._new_state(self.campaign, self.configs)
        for variant in state["variants"]:
            variant["status"] = "completed"
        out.mkdir()
        campaign_mod._checkpoint(out / "campaign.json", state)
        with patch.object(campaign_mod, "up") as up:
            resumed = campaign_mod.execute(
                self.campaign, self.configs, str(out), resume=True)
        up.assert_not_called()
        self.assertEqual(resumed["status"], "completed")

    def test_resume_rejects_a_changed_campaign_definition(self):
        out = Path(self.tmp.name) / "out"
        out.mkdir()
        state = campaign_mod._new_state(self.campaign, self.configs)
        campaign_mod._checkpoint(out / "campaign.json", state)
        changed = self.campaign.configs()
        changed[0][1].image = "registry/vantage@sha256:changed"
        with self.assertRaisesRegex(RuntimeError, "definition changed"):
            campaign_mod.execute(self.campaign, changed, str(out), resume=True)

    def test_resume_requires_an_existing_state_file(self):
        with self.assertRaisesRegex(RuntimeError, "state does not exist"):
            campaign_mod.execute(
                self.campaign, self.configs,
                str(Path(self.tmp.name) / "missing"), resume=True)

    def test_nonempty_output_without_state_is_rejected(self):
        out = Path(self.tmp.name) / "out"
        out.mkdir()
        (out / "unrelated").write_text("data")
        with self.assertRaisesRegex(RuntimeError, "not empty"):
            campaign_mod.execute(self.campaign, self.configs, str(out))

    def test_monitoring_reconfiguration_failure_is_nonfatal(self):
        patches = self.patches()
        with patches[0], patches[1], patches[2], \
             patch.object(campaign_mod.monitoring, "configure_targets",
                          side_effect=RuntimeError("prometheus unavailable")), \
             patches[4] as sweep, patches[5], patches[6], patches[7], patches[8], \
             patches[9]:
            state = campaign_mod.execute(
                self.campaign, self.configs, str(Path(self.tmp.name) / "out"))
        self.assertEqual(sweep.call_count, 2)
        self.assertEqual(state["status"], "completed")


class MatrixExecutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        Path(self.tmp.name, "key").write_text("test")
        manifest = yaml.safe_load(campaign_yaml(str(Path(self.tmp.name) / "key")))
        del manifest["base"]["nodes"]
        manifest["committee_sizes"] = [4, 8]
        manifest["rates"] = [200]
        path = Path(self.tmp.name) / "matrix.yaml"
        path.write_text(yaml.safe_dump(manifest, sort_keys=False))
        self.campaign = CampaignConfig.load(str(path))
        self.groups = [
            (nodes, child, child.configs())
            for nodes in self.campaign.committee_sizes
            for child in [self.campaign.for_committee(nodes)]
        ]

    def tearDown(self):
        self.tmp.cleanup()

    def test_committees_run_in_increasing_order_as_separate_campaigns(self):
        out = Path(self.tmp.name) / "out"
        with patch.object(matrix_mod, "execute",
                          return_value={"status": "completed"}) as execute:
            state = matrix_mod.execute_matrix(
                self.campaign, self.groups, str(out))
        self.assertEqual(
            [call.args[0].name for call in execute.call_args_list],
            ["test-campaign-n4", "test-campaign-n8"],
        )
        self.assertEqual(
            [Path(call.args[2]).name for call in execute.call_args_list],
            ["n-4", "n-8"],
        )
        self.assertEqual(state["status"], "completed")
        self.assertTrue(all(item["status"] == "completed"
                            for item in state["committees"]))
        self.assertTrue((out / "points.csv").is_file())
        self.assertNotIn(b"\r\n", (out / "points.csv").read_bytes())
        self.assertTrue((out / "README.md").is_file())

    def test_matrix_failure_is_checkpointed_before_larger_committee(self):
        out = Path(self.tmp.name) / "out"
        with patch.object(matrix_mod, "execute",
                          side_effect=RuntimeError("fleet failed")) as execute:
            with self.assertRaisesRegex(RuntimeError, "fleet failed"):
                matrix_mod.execute_matrix(
                    self.campaign, self.groups, str(out))
        execute.assert_called_once()
        state = json.loads((out / "matrix.json").read_text())
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["committees"][0]["status"], "failed")
        self.assertEqual(state["committees"][1]["status"], "pending")

if __name__ == "__main__":
    unittest.main()

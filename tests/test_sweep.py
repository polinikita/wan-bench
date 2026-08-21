import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from wanbench.config import RunConfig
from wanbench.ssh import Host
import wanbench.sweep as sweep_mod

def summary(rate, tps):
    return {
        "rate": rate,
        "tps_median": tps,
        "ordering_p50_ms_since_start": 1.0,
        "ordering_p90_ms_since_start": 2.0,
        "ordering_p50_ms_at_baseline": 0.5,
        "cpu_pct_median": 3.0,
    }

class SweepTests(unittest.TestCase):
    def setUp(self):
        self.hosts = [Host(i, f"i-{i}", f"public-{i}", f"10.0.0.{i + 1}")
                      for i in range(2)]
        self.ssh = MagicMock()
        patcher = patch.object(sweep_mod, "wait_for_progress", return_value=[])
        self.wait_for_progress = patcher.start()
        self.addCleanup(patcher.stop)
        quality = patch.object(sweep_mod, "check_progress_quality", return_value=None)
        self.check_progress_quality = quality.start()
        self.addCleanup(quality.stop)

    def test_a_barrier_pass_with_bad_quality_still_fails_the_point(self):
        self.check_progress_quality.side_effect = RuntimeError(
            "committing only 12 tx/s, below 25% of the offered 1,000 tx/s")
        with self.assertRaisesRegex(RuntimeError, "below 25%"):
            self.run_sweep([(100, 500.0)])
        self.assertEqual(self.check_progress_quality.call_count, 2,
                         "the point should be retried once, then abort")

    def run_sweep(self, values, protocol="vantage", seen_strict=None,
                  config=None, **sweep_kwargs):
        cfg = config
        if cfg is None:
            nodes = 4 if protocol == "starfish" else 2
            cfg = RunConfig(protocol=protocol, nodes=nodes, rate=100, image="image")
        else:
            nodes = cfg.nodes
        hosts = [Host(i, f"i-{i}", f"public-{i}", f"10.0.0.{i + 1}")
                 for i in range(nodes)]
        rates = [item[0] for item in values]
        points = iter(summary(*item) for item in values)
        def collect_point(*_args, **kwargs):
            if seen_strict is not None:
                seen_strict.append(kwargs["strict"])
            return next(points)

        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(sweep_mod, "up",
                          return_value=(object(), self.ssh, hosts, object())), \
             patch.object(sweep_mod, "generate_keys", return_value=[]), \
             patch.object(sweep_mod, "_reset_nodes"), \
             patch.object(sweep_mod, "deploy") as deploy, \
             patch.object(sweep_mod, "collect", side_effect=collect_point), \
             patch.object(sweep_mod.monitoring, "configure_targets") as configure, \
             patch.object(sweep_mod.faults, "clear"), \
             patch.object(sweep_mod.prepare, "clear_wan"), \
             patch.object(sweep_mod, "down"):
            result = sweep_mod.sweep(
                cfg, rates, tmp, warmup_s=0, window_s=1, **sweep_kwargs)
            self.last_configure_targets = configure
            persisted = json.loads((Path(tmp) / "sweep.json").read_text())
            return result, persisted, deploy

    def test_each_point_labels_archived_metrics(self):
        self.run_sweep(
            [(100, 100.0), (200, 200.0)],
            metric_labels={"wanbench_variant": "candidate"},
        )
        self.assertEqual(self.last_configure_targets.call_count, 2)
        labels = [call.args[3][0][1]
                  for call in self.last_configure_targets.call_args_list]
        self.assertEqual([item["wanbench_rate"] for item in labels], ["100", "200"])
        self.assertTrue(all(item["wanbench_variant"] == "candidate"
                            for item in labels))

    def test_small_tps_dip_does_not_stop(self):
        result, persisted, _ = self.run_sweep(
            [(100, 100.0), (200, 99.9), (300, 150.0)])
        self.assertEqual(len(result["points"]), 3)
        self.assertFalse(result["stopped_early"])
        self.assertEqual(persisted["status"], "completed")

    def test_exploratory_rates_disable_strict_collection(self):
        seen_strict = []
        result, persisted, _ = self.run_sweep(
            [(100, 100.0), (200, 150.0)],
            strict_through_rate=100,
            seen_strict=seen_strict,
        )
        self.assertEqual(seen_strict, [True, False])
        self.assertEqual(
            [point["strict_validation"] for point in result["points"]],
            [True, False],
        )
        self.assertEqual(persisted["strict_through_rate"], 100)
        self.assertEqual(
            [call.kwargs["enforce_rate_and_lag"]
             for call in self.check_progress_quality.call_args_list],
            [True, False],
        )

    def test_strict_optimistic_relay_requires_byzantine_commitment(self):
        cfg = RunConfig(
            protocol="autobahn-optimistic",
            nodes=40,
            rate=1_000,
            image="image",
            all_to_all=True,
            data_lane_drop_staggered_senders=13,
            data_lane_drop_staggered_width=39,
            data_lane_drop_silent_repair=True,
            data_lane_drop_headers=False,
            leader_relay_attack=True,
        )
        with self.assertRaisesRegex(RuntimeError, "200.0/325"):
            sweep_mod._check_optimistic_leader_relay_commitment(
                cfg, {"committed_uncounted_tps_median": 200.0}, strict=True)
        sweep_mod._check_optimistic_leader_relay_commitment(
            cfg, {"committed_uncounted_tps_median": 260.0}, strict=True)
        sweep_mod._check_optimistic_leader_relay_commitment(
            cfg, {"committed_uncounted_tps_median": 0.0}, strict=False)

    def test_no_early_stop_runs_the_full_ladder(self):
        result, persisted, _ = self.run_sweep(
            [(100, 100.0), (200, 1.0), (300, 2.0)],
            stop_on_drop=False,
        )
        self.assertEqual(len(result["points"]), 3)
        self.assertFalse(result["stopped_early"])
        self.assertFalse(persisted["stop_on_drop"])

    def test_material_tps_drop_stops(self):
        result, _, _ = self.run_sweep(
            [(100, 100.0), (200, 90.0), (300, 150.0)])
        self.assertEqual(len(result["points"]), 2)
        self.assertTrue(result["stopped_early"])
        self.assertIn("100.0 -> 90.0", result["stop_reason"])

    def test_below_offered_floor_stops_after_the_first_point(self):
        result, persisted, _ = self.run_sweep(
            [(100, 94.9), (200, 200.0)],
            min_offered_throughput_pct=95,
        )
        self.assertEqual(len(result["points"]), 1)
        self.assertTrue(result["stopped_early"])
        self.assertIn("below 95% of the reachable 100", result["stop_reason"])
        self.assertEqual(persisted["min_offered_throughput_pct"], 95)

    def test_offered_floor_accepts_the_boundary(self):
        result, _, _ = self.run_sweep(
            [(100, 95.0), (200, 200.0)],
            min_offered_throughput_pct=95,
        )
        self.assertEqual(len(result["points"]), 2)
        self.assertFalse(result["stopped_early"])

    def test_byzantine_unreachable_share_uses_the_reachable_floor(self):
        cfg = RunConfig(
            nodes=10,
            rate=1_000,
            image="image",
            data_lane_drop_publishers=[0, 1, 2],
            data_lane_drop_receivers=list(range(3, 10)),
            data_lane_drop_silent_repair=True,
        )
        result, persisted, _ = self.run_sweep(
            [(1_000, 665.0), (2_000, 1_330.0)],
            config=cfg,
            min_offered_throughput_pct=95,
        )

        self.assertFalse(result["stopped_early"])
        self.assertEqual(
            [point["reachable_rate"] for point in result["points"]],
            [700, 1_400],
        )
        self.assertEqual(
            [point["unreachable_rate"] for point in result["points"]],
            [300, 600],
        )
        self.assertEqual(
            [point["reachable_throughput_pct"] for point in result["points"]],
            [95.0, 95.0],
        )
        self.assertEqual(persisted["points"][0]["reachable_rate"], 700)
        labels = [call.args[3][0][1] for call in self.last_configure_targets.call_args_list]
        self.assertEqual(
            [label["wanbench_reachable_rate"] for label in labels],
            ["700", "1400"],
        )

    def test_zero_progress_stops_immediately(self):
        result, _, _ = self.run_sweep([(100, 0.0), (200, 100.0)])
        self.assertEqual(len(result["points"]), 1)
        self.assertTrue(result["stopped_early"])
        self.assertIn("no committed progress", result["stop_reason"])

    def test_invalid_rate_is_rejected_before_up(self):
        cfg = RunConfig(nodes=50, rate=100, image="image")
        with tempfile.TemporaryDirectory() as tmp, patch.object(sweep_mod, "up") as up:
            with self.assertRaisesRegex(ValueError, "divisible"):
                sweep_mod.sweep(cfg, [51], tmp)
            up.assert_not_called()

    def test_starfish_reuses_genesis_without_generic_keygen(self):
        with patch.object(sweep_mod, "generate_keys") as generate:
            _, _, deploy = self.run_sweep(
                [(100, 80.0), (200, 100.0)], protocol="starfish")
            generate.assert_not_called()
        self.assertEqual(
            [call.kwargs["reuse_genesis"] for call in deploy.call_args_list],
            [False, True],
        )

    def test_timeline_is_recorded_in_sweep_json(self):
        _, persisted, _ = self.run_sweep(
            [(100, 100.0), (200, 150.0)])
        timeline = persisted["timeline"]
        self.assertEqual(set(timeline), {"steps", "points", "teardown_s", "total_s"})

        step_names = [name for name, _ in timeline["steps"]]
        self.assertEqual(step_names, ["up", "keygen"])
        for name, seconds in timeline["steps"]:
            self.assertIsInstance(name, str)
            self.assertIsInstance(seconds, int)

        self.assertEqual(len(timeline["points"]), 2)
        for point, rate in zip(timeline["points"], (100, 200)):
            self.assertEqual(point["rate"], rate)
            self.assertIsInstance(point["deploy_s"], int)
            self.assertIsInstance(point["measure_s"], int)

        self.assertIsInstance(timeline["teardown_s"], int)
        self.assertIsInstance(timeline["total_s"], int)

    def test_reports_point_count_time_and_attempts_on_one_line(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.run_sweep([(100, 99.5)])
        self.assertRegex(
            output.getvalue(),
            r"sweep: point 1/1 completed in \d+s; attempts=1; "
            r"offered=100, reachable=100, rate=100, committed=99\.5 tx/s",
        )

    def test_adversarial_sweep_keeps_useful_rate_fixed(self):
        cfg = RunConfig(
            nodes=2,
            rate=100,
            correct_load_only=True,
            image="image",
            data_lane_drop_publishers=[0],
            data_lane_drop_receivers=[1],
        )
        points = iter([summary(100, 100.0), summary(100, 80.0)])
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(sweep_mod, "up",
                          return_value=(object(), self.ssh, self.hosts, object())), \
             patch.object(sweep_mod, "generate_keys", return_value=[]), \
             patch.object(sweep_mod, "_reset_nodes"), \
             patch.object(sweep_mod, "deploy"), \
             patch.object(sweep_mod, "collect",
                          side_effect=lambda *a, **k: next(points)), \
             patch.object(sweep_mod.monitoring, "configure_targets") as configure, \
             patch.object(sweep_mod.faults, "clear"), \
             patch.object(sweep_mod.prepare, "clear_wan"), \
             patch.object(sweep_mod, "down"):
            result = sweep_mod.sweep(
                cfg,
                [0, 100],
                tmp,
                sweep_field="adversarial_rate",
                warmup_s=0,
                window_s=1,
                stop_on_drop=False,
            )

        self.assertEqual([point["rate"] for point in result["points"]], [100, 100])
        self.assertEqual(
            [point["adversarial_rate"] for point in result["points"]],
            [0, 100],
        )
        labels = [call.args[3][0][1] for call in configure.call_args_list]
        self.assertEqual(
            [label["wanbench_adversarial_rate"] for label in labels],
            ["0", "100"],
        )

    def test_failure_is_checkpointed_and_tears_down(self):
        cfg = RunConfig(nodes=2, rate=100, image="image")
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(sweep_mod, "up",
                          return_value=(object(), self.ssh, self.hosts, object())), \
             patch.object(sweep_mod, "generate_keys", return_value=[]), \
             patch.object(sweep_mod, "_reset_nodes"), \
             patch.object(sweep_mod, "deploy"), \
             patch.object(sweep_mod, "collect", side_effect=RuntimeError("metrics down")), \
             patch.object(sweep_mod.faults, "clear"), \
             patch.object(sweep_mod.prepare, "clear_wan"), \
             patch.object(sweep_mod, "down") as down:
            with self.assertRaisesRegex(RuntimeError, "metrics down"):
                sweep_mod.sweep(cfg, [100], tmp, warmup_s=0, window_s=1)
            saved = json.loads((Path(tmp) / "sweep.json").read_text())
            self.assertEqual(saved["status"], "failed")
            self.assertIn("metrics down", saved["error"])
            down.assert_called_once_with(cfg)

    def test_borrowed_fleet_is_not_started_or_torn_down(self):
        cfg = RunConfig(nodes=2, rate=100, image="image")
        control = Host(2, "i-control", "public-control", "10.0.0.3")
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(sweep_mod, "up") as up, \
             patch.object(sweep_mod, "generate_keys", return_value=[]), \
             patch.object(sweep_mod, "_reset_nodes"), \
             patch.object(sweep_mod, "deploy"), \
             patch.object(sweep_mod, "collect", return_value=summary(100, 100.0)), \
             patch.object(sweep_mod.faults, "clear") as clear_faults, \
             patch.object(sweep_mod.prepare, "clear_wan") as clear_wan, \
             patch.object(sweep_mod, "down") as down:
            result = sweep_mod.sweep(
                cfg, [100], tmp, warmup_s=0, window_s=1,
                fleet=(object(), self.ssh, self.hosts, control),
            )
            self.assertTrue((Path(tmp) / "effective-config.yaml").is_file())
        up.assert_not_called()
        down.assert_not_called()
        clear_faults.assert_not_called()
        clear_wan.assert_not_called()
        self.assertEqual(result["timeline"]["teardown_s"], 0)

    def test_a_catchable_kill_still_tears_the_fleet_down(self):
        from wanbench.cli import Killed
        cfg = RunConfig(nodes=2, rate=100, image="image")
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(sweep_mod, "up",
                          return_value=(object(), self.ssh, self.hosts, object())), \
             patch.object(sweep_mod, "generate_keys", return_value=[]), \
             patch.object(sweep_mod, "_reset_nodes"), \
             patch.object(sweep_mod, "deploy"), \
             patch.object(sweep_mod, "collect", side_effect=Killed("signal SIGTERM")), \
             patch.object(sweep_mod.faults, "clear"), \
             patch.object(sweep_mod.prepare, "clear_wan"), \
             patch.object(sweep_mod, "down") as down:
            with self.assertRaises(Killed):
                sweep_mod.sweep(cfg, [100], tmp, warmup_s=0, window_s=1)
            saved = json.loads((Path(tmp) / "sweep.json").read_text())
        down.assert_called_once_with(cfg)
        self.assertEqual(saved["status"], "failed")
        self.assertIn("SIGTERM", saved["error"])
        self.assertEqual(saved["points"], [])

    def test_stalled_nodes_are_never_relaunched_individually(self):
        cfg = RunConfig(nodes=2, rate=100, image="image")
        self.wait_for_progress.side_effect = [[self.hosts[1]], []]
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(sweep_mod, "up",
                          return_value=(object(), self.ssh, self.hosts, object())), \
             patch.object(sweep_mod, "generate_keys", return_value=[]), \
             patch.object(sweep_mod, "_reset_nodes") as reset, \
             patch.object(sweep_mod, "deploy") as deploy, \
             patch.object(sweep_mod, "collect", return_value=summary(100, 100.0)), \
             patch.object(sweep_mod.faults, "clear"), \
             patch.object(sweep_mod.prepare, "clear_wan"), \
             patch.object(sweep_mod, "down"):
            sweep_mod.sweep(cfg, [100], tmp, warmup_s=0, window_s=1)
        self.assertFalse(hasattr(sweep_mod, "launch_nodes"))
        self.assertEqual(deploy.call_count, 2)
        self.assertEqual(reset.call_count, 2)

    def test_barrier_failure_on_both_attempts_aborts(self):
        cfg = RunConfig(nodes=2, rate=100, image="image")
        self.wait_for_progress.return_value = [self.hosts[1]]
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(sweep_mod, "up",
                          return_value=(object(), self.ssh, self.hosts, object())), \
             patch.object(sweep_mod, "generate_keys", return_value=[]), \
             patch.object(sweep_mod, "_reset_nodes"), \
             patch.object(sweep_mod, "deploy"), \
             patch.object(sweep_mod, "collect") as collect_mock, \
             patch.object(sweep_mod.faults, "clear"), \
             patch.object(sweep_mod.prepare, "clear_wan"), \
             patch.object(sweep_mod, "down") as down:
            with self.assertRaisesRegex(RuntimeError, "progress barrier"):
                sweep_mod.sweep(cfg, [100], tmp, warmup_s=0, window_s=1)
            saved = json.loads((Path(tmp) / "sweep.json").read_text())
        collect_mock.assert_not_called()
        self.assertEqual(saved["status"], "failed")
        self.assertEqual(saved["points"], [])
        down.assert_called_once_with(cfg)

    def test_retry_scrapes_go_to_their_own_directory(self):
        cfg = RunConfig(nodes=2, rate=100, image="image")
        seen = []

        def flaky(_ssh, _cfg, _control, _hosts, outdir, **kwargs):
            seen.append(Path(outdir).name)
            if len(seen) == 1:
                raise RuntimeError("node(s) [1] committed nothing during measurement")
            return summary(100, 100.0)

        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(sweep_mod, "up",
                          return_value=(object(), self.ssh, self.hosts, object())), \
             patch.object(sweep_mod, "generate_keys", return_value=[]), \
             patch.object(sweep_mod, "_reset_nodes"), \
             patch.object(sweep_mod, "deploy"), \
             patch.object(sweep_mod, "collect", side_effect=flaky), \
             patch.object(sweep_mod.faults, "clear"), \
             patch.object(sweep_mod.prepare, "clear_wan"), \
             patch.object(sweep_mod, "down"):
            sweep_mod.sweep(cfg, [100], tmp, warmup_s=0, window_s=1)
        self.assertEqual(seen, ["rate-100", "rate-100-attempt2"])

    def test_deadman_switch_is_renewed_at_every_point(self):
        cfg = RunConfig(nodes=2, rate=100, image="image")
        points = iter([summary(100, 100.0), summary(200, 150.0)])
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(sweep_mod, "up",
                          return_value=(object(), self.ssh, self.hosts, object())), \
             patch.object(sweep_mod, "generate_keys", return_value=[]), \
             patch.object(sweep_mod, "_reset_nodes"), \
             patch.object(sweep_mod, "deploy"), \
             patch.object(sweep_mod, "collect",
                          side_effect=lambda *a, **k: next(points)), \
             patch.object(sweep_mod.faults, "clear"), \
             patch.object(sweep_mod.prepare, "clear_wan"), \
             patch.object(sweep_mod.prepare, "arm_deadman") as arm, \
             patch.object(sweep_mod, "down"):
            sweep_mod.sweep(cfg, [100, 200], tmp, warmup_s=0, window_s=1)
        self.assertEqual(arm.call_count, 2)
        for call in arm.call_args_list:
            self.assertEqual(call.args[2], cfg.deadman_minutes)

    def test_strict_failure_retries_the_point_once_and_recovers(self):
        cfg = RunConfig(nodes=2, rate=100, image="image")
        attempts = []

        def flaky(*args, **kwargs):
            attempts.append(1)
            if len(attempts) == 1:
                raise RuntimeError(
                    "node(s) [1] committed nothing during measurement")
            return summary(100, 100.0)

        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(sweep_mod, "up",
                          return_value=(object(), self.ssh, self.hosts, object())), \
             patch.object(sweep_mod, "generate_keys", return_value=[]), \
             patch.object(sweep_mod, "_reset_nodes") as reset, \
             patch.object(sweep_mod, "deploy") as deploy, \
             patch.object(sweep_mod, "collect", side_effect=flaky), \
             patch.object(sweep_mod.faults, "clear"), \
             patch.object(sweep_mod.prepare, "clear_wan"), \
             patch.object(sweep_mod, "down"):
            result = sweep_mod.sweep(cfg, [100], tmp, warmup_s=0, window_s=1)
            saved = json.loads((Path(tmp) / "sweep.json").read_text())
        self.assertEqual(len(result["points"]), 1)
        self.assertEqual(saved["status"], "completed")
        self.assertEqual(len(attempts), 2)
        self.assertEqual(deploy.call_count, 2)
        self.assertEqual(reset.call_count, 2)

    def test_terminal_point_is_measured_once_while_earlier_rungs_retry(self):
        """A failure above an accepted rate is the knee, not a flaky fleet."""
        cfg = RunConfig(nodes=2, rate=100, image="image")
        calls = []

        def by_rate(ssh, run_cfg, *args, **kwargs):
            calls.append(run_cfg.rate)
            if run_cfg.rate == 100:
                return summary(100, 100.0)
            raise RuntimeError(
                "node(s) [1] committed nothing during measurement")

        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(sweep_mod, "up",
                          return_value=(object(), self.ssh, self.hosts, object())), \
             patch.object(sweep_mod, "generate_keys", return_value=[]), \
             patch.object(sweep_mod, "_reset_nodes"), \
             patch.object(sweep_mod, "deploy"), \
             patch.object(sweep_mod, "collect", side_effect=by_rate), \
             patch.object(sweep_mod, "dump_failure_scrapes"), \
             patch.object(sweep_mod.faults, "clear"), \
             patch.object(sweep_mod.prepare, "clear_wan"), \
             patch.object(sweep_mod, "down"):
            with self.assertRaises(RuntimeError):
                sweep_mod.sweep(cfg, [100, 200], tmp, warmup_s=0, window_s=1,
                                point_attempts=2, terminal_point_attempts=1,
                                stop_on_drop=True)

        # 100 accepted on its first attempt; 200 tried exactly once despite
        # point_attempts=2, because the ladder already had an accepted point.
        self.assertEqual(calls, [100, 200])

    def test_first_rung_still_uses_the_full_attempt_budget(self):
        """With no accepted point yet, a failure is treated as infrastructure."""
        cfg = RunConfig(nodes=2, rate=100, image="image")
        attempts = []

        def flaky(*args, **kwargs):
            attempts.append(1)
            if len(attempts) == 1:
                raise RuntimeError(
                    "node(s) [1] committed nothing during measurement")
            return summary(100, 100.0)

        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(sweep_mod, "up",
                          return_value=(object(), self.ssh, self.hosts, object())), \
             patch.object(sweep_mod, "generate_keys", return_value=[]), \
             patch.object(sweep_mod, "_reset_nodes"), \
             patch.object(sweep_mod, "deploy"), \
             patch.object(sweep_mod, "collect", side_effect=flaky), \
             patch.object(sweep_mod, "dump_failure_scrapes"), \
             patch.object(sweep_mod.faults, "clear"), \
             patch.object(sweep_mod.prepare, "clear_wan"), \
             patch.object(sweep_mod, "down"):
            result = sweep_mod.sweep(cfg, [100], tmp, warmup_s=0, window_s=1,
                                     point_attempts=2, terminal_point_attempts=1)

        self.assertEqual(len(result["points"]), 1)
        self.assertEqual(len(attempts), 2)

    def test_terminal_point_attempts_must_be_positive(self):
        cfg = RunConfig(nodes=2, rate=100, image="image")
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "terminal_point_attempts"):
                sweep_mod.sweep(cfg, [100], tmp, terminal_point_attempts=0)

    def test_deploy_timeout_retries_the_full_point(self):
        cfg = RunConfig(nodes=2, rate=100, image="image")
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(sweep_mod, "up",
                          return_value=(object(), self.ssh, self.hosts, object())), \
             patch.object(sweep_mod, "generate_keys", return_value=[]), \
             patch.object(sweep_mod, "_reset_nodes") as reset, \
             patch.object(sweep_mod, "deploy",
                          side_effect=[subprocess.TimeoutExpired("ssh", 120), None]) as deploy, \
             patch.object(sweep_mod, "collect", return_value=summary(100, 100.0)), \
             patch.object(sweep_mod, "dump_failure_scrapes") as dump, \
             patch.object(sweep_mod.faults, "clear"), \
             patch.object(sweep_mod.prepare, "clear_wan"), \
             patch.object(sweep_mod, "down"):
            result = sweep_mod.sweep(cfg, [100], tmp, warmup_s=0, window_s=1)

        self.assertEqual(result["status"], "completed")
        self.assertEqual(reset.call_count, 2)
        self.assertEqual(deploy.call_count, 2)
        self.assertEqual(dump.call_args.args[-1], "failure")

    def test_strict_failure_twice_aborts_without_recording_the_point(self):
        cfg = RunConfig(nodes=2, rate=100, image="image")
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(sweep_mod, "up",
                          return_value=(object(), self.ssh, self.hosts, object())), \
             patch.object(sweep_mod, "generate_keys", return_value=[]), \
             patch.object(sweep_mod, "_reset_nodes"), \
             patch.object(sweep_mod, "deploy"), \
             patch.object(sweep_mod, "collect", side_effect=RuntimeError(
                 "node(s) [1] committed nothing during measurement")) as collect, \
             patch.object(sweep_mod.faults, "clear"), \
             patch.object(sweep_mod.prepare, "clear_wan"), \
             patch.object(sweep_mod, "down"):
            with self.assertRaisesRegex(RuntimeError, "committed nothing"):
                sweep_mod.sweep(cfg, [100], tmp, warmup_s=0, window_s=1)
            saved = json.loads((Path(tmp) / "sweep.json").read_text())
        self.assertEqual(saved["status"], "failed")
        self.assertEqual(saved["points"], [])
        self.assertEqual(collect.call_count, 2)

if __name__ == "__main__":
    unittest.main()

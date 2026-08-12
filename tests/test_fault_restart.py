import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from wanbench import timeseries
from wanbench.collect import collect
from wanbench.config import FaultConfig, RunConfig
from wanbench.faults import restart
from wanbench.run import _fault_delay_s, _schedule_fault
from wanbench.ssh import Host


def metrics(port, committed, cpu):
    return f"""# WANBENCH_OK {port}
committed_transactions {committed}
committed_bytes {committed * 512}
transaction_committed_latency{{v="p50"}} 500
process_cpu_seconds_total {cpu}
process_resident_memory_bytes 1000000
bytes_sent_total {committed * 10}
network_messages_sent_total {committed}
"""


class RestartTests(unittest.TestCase):
    def setUp(self):
        self.hosts = [Host(i, f"i-{i}", "public", f"10.0.0.{i + 1}")
                      for i in range(4)]

    def test_restart_targets_only_the_listed_nodes_with_docker_start(self):
        ssh = MagicMock()
        restart(ssh, self.hosts, [1, 3])
        targeted, command = ssh.fanout.call_args.args
        self.assertEqual([h.index for h in targeted], [1, 3])
        self.assertIn("docker start wanbench-node", command)
        # Pre-crash logs must survive the entrypoint's truncating redirects.
        self.assertIn("pre-restart", command)

    def test_restart_rejects_unknown_indices(self):
        with self.assertRaisesRegex(ValueError, r"\[9\]"):
            restart(MagicMock(), self.hosts, [9])


class FaultConfigTests(unittest.TestCase):
    def _cfg(self, **fault):
        return RunConfig(nodes=4, rate=100, image="image", key_name="k",
                         fault=FaultConfig(**fault))

    def test_negative_timing_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "must be >= 0"):
            self._cfg(kind="crash", nodes=[1], at_s=-1).validate()

    def test_crash_restart_cycle_must_fit_the_measured_window(self):
        cfg = self._cfg(kind="crash", nodes=[1], at_s=60, for_s=60)
        cfg.duration_s = 120
        with self.assertRaisesRegex(ValueError, "must fit"):
            cfg.validate()

    def test_crash_restart_cycle_within_window_is_accepted(self):
        cfg = self._cfg(kind="crash", nodes=[1], at_s=20, for_s=20)
        cfg.duration_s = 130
        cfg.validate()

    def test_crash_must_leave_at_least_one_node(self):
        with self.assertRaisesRegex(ValueError, "at least one node"):
            self._cfg(kind="crash", nodes=[0, 1, 2, 3]).validate()


class FaultScheduleTests(unittest.TestCase):
    def test_delay_is_anchored_to_the_metrics_window(self):
        # Window opens at t=100s; at_s=20 means the fault fires at t=120s.
        self.assertEqual(_fault_delay_s(100_000, 20, now_s=50.0), 70.0)
        # A late scheduler never sleeps negatively.
        self.assertEqual(_fault_delay_s(100_000, 20, now_s=130.0), 0.0)
        # Without an anchor, at_s counts from now.
        self.assertEqual(_fault_delay_s(None, 20, now_s=42.0), 20.0)

    def _run_schedule(self, cfg, tmp):
        with patch("wanbench.run.time.sleep"), \
             patch("wanbench.run.faults") as mocked:
            thread = _schedule_fault(MagicMock(), cfg, [], tmp)
            thread.join(timeout=5)
        return mocked, json.loads((Path(tmp) / "fault-timeline.json").read_text())

    def test_crash_with_for_s_restarts_and_records_the_timeline(self):
        cfg = RunConfig(nodes=4, rate=100, image="image",
                        fault=FaultConfig(kind="crash", nodes=[1, 2],
                                          at_s=20, for_s=20))
        with tempfile.TemporaryDirectory() as tmp:
            mocked, timeline = self._run_schedule(cfg, tmp)
        mocked.apply_from_config.assert_called_once()
        mocked.restart.assert_called_once()
        self.assertEqual(mocked.restart.call_args.args[2], [1, 2])
        self.assertIsNotNone(timeline["down_ms"])
        self.assertIsNotNone(timeline["up_ms"])
        self.assertIsNone(timeline["error"])

    def test_permanent_crash_never_restarts(self):
        cfg = RunConfig(nodes=4, rate=100, image="image",
                        fault=FaultConfig(kind="crash", nodes=[1], for_s=0))
        with tempfile.TemporaryDirectory() as tmp:
            mocked, timeline = self._run_schedule(cfg, tmp)
        mocked.restart.assert_not_called()
        self.assertIsNone(timeline["up_ms"])

    def test_fault_failure_is_recorded_not_swallowed(self):
        cfg = RunConfig(nodes=4, rate=100, image="image",
                        fault=FaultConfig(kind="crash", nodes=[1], for_s=0))
        with tempfile.TemporaryDirectory() as tmp:
            with patch("wanbench.run.time.sleep"), \
                 patch("wanbench.run.faults") as mocked:
                mocked.apply_from_config.side_effect = RuntimeError("ssh down")
                thread = _schedule_fault(MagicMock(), cfg, [], tmp)
                thread.join(timeout=5)
            timeline = json.loads((Path(tmp) / "fault-timeline.json").read_text())
        self.assertIn("ssh down", timeline["error"])


class CrashCohortCollectTests(unittest.TestCase):
    def test_crash_nodes_are_excluded_from_medians_and_reported(self):
        cfg = RunConfig(nodes=2, rate=100, image="image", metrics_port=6003,
                        fault=FaultConfig(kind="crash", nodes=[1], at_s=20,
                                          for_s=20))
        hosts = [Host(0, "i-0", "public", "10.0.0.1"),
                 Host(1, "i-1", "public", "10.0.0.2")]
        base = {0: metrics(6007, 10, 1) + metrics(6003, 0, 2),
                1: metrics(6007, 10, 1) + metrics(6003, 0, 2)}
        # Node 1 restarted mid-run: its post-restart delta is positive but
        # covers a partial window (would bias the median to 75 tx/s).
        final = {0: metrics(6007, 1010, 3) + metrics(6003, 0, 4),
                 1: metrics(6007, 510, 2) + metrics(6003, 0, 3)}
        with tempfile.TemporaryDirectory() as tmp, \
             patch("wanbench.collect.time.sleep"), \
             patch("wanbench.collect.time.monotonic", side_effect=[0, 0, 0, 10]), \
             patch("wanbench.collect.scrape_all", side_effect=[base, final]), \
             patch("wanbench.prepare.report_netem_drops", return_value=0):
            result = collect(None, cfg, None, hosts, tmp,
                             baseline_at=0, final_at=10)
        self.assertEqual(result["excluded_nodes"], [1])
        self.assertEqual(result["nodes_in_medians"], 1)
        self.assertEqual(result["fault_nodes"], [1])
        self.assertEqual(result["tps_median"], 100.0)


class TimeseriesTests(unittest.TestCase):
    def test_queries_use_the_protocol_families_counters(self):
        vantage = timeseries.queries(RunConfig(nodes=4, rate=100, image="i"))
        self.assertIn("committed_transactions", vantage["committee_tps_p50"])
        starfish = timeseries.queries(
            RunConfig(protocol="starfish", nodes=4, rate=100, image="i",
                      protocol_flags=["--consensus", "bluestreak"]))
        self.assertIn("sequenced_transactions_total",
                      starfish["committee_tps_p50"])
        self.assertIn("commit_index_min", starfish)

    def test_dump_writes_one_series_per_query(self):
        cfg = RunConfig(nodes=4, rate=100, image="i", duration_s=60)
        cfg.metrics_active_at_ms = 1_000_000
        ssh = MagicMock()
        ssh.run.return_value = json.dumps({
            "status": "success",
            "data": {"result": [{"metric": {}, "values": [[1, "2"]]}]},
        })
        control = Host(-1, "i-c", "public", "10.0.0.9")
        with tempfile.TemporaryDirectory() as tmp:
            path = timeseries.dump(ssh, cfg, control, tmp, end_s=2_000.0)
            artifact = json.loads(path.read_text())
        expected = set(timeseries.queries(cfg))
        self.assertEqual(set(artifact["series"]), expected)
        self.assertEqual(artifact["errors"], {})
        self.assertEqual(ssh.run.call_count, len(expected))
        self.assertIn("query_range", ssh.run.call_args.args[1])


if __name__ == "__main__":
    unittest.main()

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from wanbench.collect import (_bandwidth_fields, _metrics_ports, _scrape_one,
                             _starfish_memory_fields,
                             _successful_ports, _split_by_port, _wait_for_window_s,
                             _worker_health_fields, check_progress_quality,
                             collect, scrape_all)
from wanbench.config import RunConfig
from wanbench.ssh import Host

def metrics(port, committed, cpu):
    return f"""# WANBENCH_OK {port}
committed_transactions {committed}
committed_bytes {committed * 512}
transaction_committed_latency{{v="p50"}} 500
transaction_committed_latency{{v="p90"}} 900
transaction_committed_latency{{v="p99"}} 1500
process_cpu_seconds_total {cpu}
process_resident_memory_bytes 1000000
bytes_sent_total {committed * 10}
network_messages_sent_total {committed}
"""

class CollectTests(unittest.TestCase):
    def setUp(self):
        self.cfg = RunConfig(nodes=1, rate=100, image="image", metrics_port=6003)
        self.hosts = [Host(0, "i-0", "public", "10.0.0.1")]

    def test_strict_collection_rejects_dead_endpoint(self):
        dead = {0: "# WANBENCH_FAILED 6007\n# WANBENCH_OK 6003\n"}
        with tempfile.TemporaryDirectory() as tmp, \
             patch("wanbench.collect.time.sleep"), \
             patch("wanbench.collect.scrape_all", return_value=dead):
            with self.assertRaisesRegex(RuntimeError, "6007"):
                collect(None, self.cfg, None, self.hosts, tmp,
                        baseline_at=0, final_at=10, strict=True)

    def test_valid_collection_emits_strict_json_and_explicit_latency_units(self):
        base = {0: metrics(6007, 10, 1) + metrics(6003, 0, 2)}
        final = {0: metrics(6007, 1010, 3) + metrics(6003, 0, 4)}
        with tempfile.TemporaryDirectory() as tmp, \
             patch("wanbench.collect.time.sleep"), \
             patch("wanbench.collect.time.monotonic", side_effect=[0, 0, 0, 10]), \
             patch("wanbench.collect.scrape_all", side_effect=[base, final]), \
             patch("wanbench.prepare.report_netem_drops", return_value=7):
            result = collect(None, self.cfg, None, self.hosts, tmp,
                             baseline_at=0, final_at=10, strict=True)
            raw = (Path(tmp) / "summary.json").read_text()
        self.assertEqual(result["tps_median"], 100.0)
        self.assertEqual(result["ordering_p50_ms_since_start"], 0.5)
        self.assertEqual(result["healthy_nodes_final"], 1)
        self.assertEqual(result["netem_dropped_packets"], 7)
        self.assertEqual(result["bandwidth_efficiency_p50"], round(10 / 512, 4))
        self.assertEqual(json.loads(raw)["ordering_p50_ms_since_start"], 0.5)

    def test_bandwidth_efficiency_is_reduced_per_validator(self):
        cfg = RunConfig(nodes=2, rate=100, image="image")
        got = _bandwidth_fields(
            cfg,
            committed=[100, 100],
            committed_bytes=[51_200, 51_200],
            sent_bytes=[60_000, 70_000],
        )
        self.assertEqual(got["committed_bytes_per_tx_p50"], 512.0)
        self.assertEqual(got["wire_bytes_per_tx_p50"], 650.0)
        self.assertEqual(got["bandwidth_efficiency_p50"], 1.2695)
        self.assertEqual(got["estimated_payload_efficiency"], 0.5)
        self.assertEqual(got["estimated_non_payload_bytes_per_tx_p50"], 394.0)
        self.assertEqual(got["estimated_non_payload_efficiency_p50"], 0.7695)

    def test_bandwidth_efficiency_is_null_without_payload_counter(self):
        cfg = RunConfig(nodes=2, rate=100, image="image")
        got = _bandwidth_fields(cfg, [100, 100], [0, 0], [60_000, 70_000])
        self.assertIsNone(got["bandwidth_efficiency_p50"])
        self.assertEqual(got["estimated_payload_efficiency"], 0.5)

    def test_missing_latency_is_json_null_not_nan(self):
        text = "# WANBENCH_OK 6007\n# WANBENCH_OK 6003\n"
        snapshots = [{0: text}, {0: text}]
        with tempfile.TemporaryDirectory() as tmp, \
             patch("wanbench.collect.time.sleep"), \
             patch("wanbench.collect.time.monotonic", side_effect=[0, 0, 0, 10]), \
             patch("wanbench.collect.scrape_all", side_effect=snapshots):
            result = collect(None, self.cfg, None, self.hosts, tmp,
                             baseline_at=0, final_at=10)
            raw = (Path(tmp) / "summary.json").read_text()
        self.assertIsNone(result["ordering_p50_ms_since_start"])
        self.assertNotIn("NaN", raw)

    def test_rates_divide_by_the_nodes_own_active_window_not_wall_clock(self):
        def with_clock(port, committed, cpu, active_s):
            return (metrics(port, committed, cpu)
                    + f"metrics_active_seconds {active_s}\n")

        base = {0: with_clock(6007, 10, 1, 100.0) + metrics(6003, 0, 2)}
        final = {0: with_clock(6007, 1010, 3, 105.0) + metrics(6003, 0, 4)}
        with tempfile.TemporaryDirectory() as tmp, \
             patch("wanbench.collect.time.sleep"), \
             patch("wanbench.collect.time.monotonic", side_effect=[0, 0, 0, 10]), \
             patch("wanbench.collect.scrape_all", side_effect=[base, final]):
            result = collect(None, self.cfg, None, self.hosts, tmp,
                             baseline_at=0, final_at=10, strict=True)
        self.assertEqual(result["tps_median"], 200.0)
        self.assertEqual(result["window_s"], 5.0)
        self.assertEqual(result["window_source"], "node_clock")
        self.assertEqual(result["wall_window_s"], 10.0)

    def test_active_window_is_not_doubled_when_both_processes_publish_it(self):
        def with_clock(port, committed, cpu, active_s):
            return (metrics(port, committed, cpu)
                    + f"metrics_active_seconds {active_s}\n")

        base = {0: with_clock(6007, 10, 1, 100.0) + with_clock(6003, 0, 2, 100.0)}
        final = {0: with_clock(6007, 1010, 3, 105.0) + with_clock(6003, 0, 4, 105.0)}
        with tempfile.TemporaryDirectory() as tmp, \
             patch("wanbench.collect.time.sleep"), \
             patch("wanbench.collect.time.monotonic", side_effect=[0, 0, 0, 10]), \
             patch("wanbench.collect.scrape_all", side_effect=[base, final]):
            result = collect(None, self.cfg, None, self.hosts, tmp,
                             baseline_at=0, final_at=10, strict=True)
        self.assertEqual(result["window_s"], 5.0, "window must not double")
        self.assertEqual(result["tps_median"], 200.0, "rate must not halve")
        self.assertEqual(result["window_source"], "node_clock")

    def test_rates_fall_back_to_wall_clock_when_the_node_has_no_clock(self):
        base = {0: metrics(6007, 10, 1) + metrics(6003, 0, 2)}
        final = {0: metrics(6007, 1010, 3) + metrics(6003, 0, 4)}
        with tempfile.TemporaryDirectory() as tmp, \
             patch("wanbench.collect.time.sleep"), \
             patch("wanbench.collect.time.monotonic", side_effect=[0, 0, 0, 10]), \
             patch("wanbench.collect.scrape_all", side_effect=[base, final]):
            result = collect(None, self.cfg, None, self.hosts, tmp,
                             baseline_at=0, final_at=10, strict=True)
        self.assertEqual(result["tps_median"], 100.0)
        self.assertEqual(result["window_source"], "wall_clock")

    def test_material_latency_is_reported_alongside_ordering(self):
        def with_material(port, committed, cpu, ordering_us, material_us):
            return metrics(port, committed, cpu).replace(
                'transaction_committed_latency{v="p50"} 500',
                f'transaction_committed_latency{{v="p50"}} {ordering_us}\n'
                f'transaction_committed_latency{{v="p95"}} {ordering_us}\n'
                f'transaction_materialised_latency{{v="p50"}} {material_us}\n'
                f'transaction_materialised_latency{{v="p90"}} {material_us}\n'
                f'transaction_materialised_latency{{v="p95"}} {material_us}\n'
                f'transaction_materialised_latency{{v="p99"}} {material_us}',
            )

        base = {0: with_material(6007, 10, 1, 400, 500) + metrics(6003, 0, 2)}
        final = {0: with_material(6007, 1010, 3, 400, 500) + metrics(6003, 0, 4)}
        with tempfile.TemporaryDirectory() as tmp, \
             patch("wanbench.collect.time.sleep"), \
             patch("wanbench.collect.time.monotonic", side_effect=[0, 0, 0, 10]), \
             patch("wanbench.collect.scrape_all", side_effect=[base, final]):
            result = collect(None, self.cfg, None, self.hosts, tmp,
                             baseline_at=0, final_at=10, strict=True)
            raw = (Path(tmp) / "summary.json").read_text()
        self.assertEqual(result["ordering_p50_ms_since_start"], 0.4)
        self.assertEqual(result["ordering_p95_ms_since_start"], 0.4)
        self.assertEqual(result["material_p50_ms_since_start"], 0.5)
        self.assertEqual(result["material_p99_ms_since_start"], 0.5)
        self.assertEqual(result["material_p50_ms_at_baseline"], 0.5)
        self.assertGreaterEqual(result["material_p50_ms_since_start"],
                                result["ordering_p50_ms_since_start"])
        self.assertNotIn("NaN", raw)

    def test_strict_collection_rejects_node_whose_committed_never_advances(self):
        hosts = [Host(0, "i-0", "public", "10.0.0.1"), Host(1, "i-1", "public", "10.0.0.2")]
        cfg = RunConfig(nodes=2, rate=100, image="image", metrics_port=6003)
        base = {
            0: metrics(6007, 10, 1) + metrics(6003, 0, 2),
            1: metrics(6007, 10, 1) + metrics(6003, 0, 2),
        }
        final = {
            0: metrics(6007, 1010, 3) + metrics(6003, 0, 4),
            1: metrics(6007, 10, 3) + metrics(6003, 0, 4),  # stalled: no progress
        }
        with tempfile.TemporaryDirectory() as tmp, \
             patch("wanbench.collect.time.sleep"), \
             patch("wanbench.collect.time.monotonic", side_effect=[0, 0, 0, 10]), \
             patch("wanbench.collect.scrape_all", side_effect=[base, final]):
            with self.assertRaisesRegex(RuntimeError, r"\[1\] committed nothing"):
                collect(None, cfg, None, hosts, tmp,
                        baseline_at=0, final_at=10, strict=True)

    def test_strict_collection_rejects_one_slow_validator(self):
        hosts = [Host(0, "i-0", "public", "10.0.0.1"),
                 Host(1, "i-1", "public", "10.0.0.2")]
        cfg = RunConfig(nodes=2, rate=100, image="image", metrics_port=6003)
        base = {
            0: metrics(6007, 10, 1) + metrics(6003, 0, 2),
            1: metrics(6007, 10, 1) + metrics(6003, 0, 2),
        }
        final = {
            0: metrics(6007, 1010, 3) + metrics(6003, 0, 4),
            1: metrics(6007, 210, 3) + metrics(6003, 0, 4),
        }
        with tempfile.TemporaryDirectory() as tmp, \
             patch("wanbench.collect.time.sleep"), \
             patch("wanbench.collect.time.monotonic", side_effect=[0, 0, 0, 10]), \
             patch("wanbench.collect.scrape_all", side_effect=[base, final]):
            with self.assertRaisesRegex(RuntimeError, r"rates below 80%.*\(1, 20\.0\)"):
                collect(None, cfg, None, hosts, tmp,
                        baseline_at=0, final_at=10, strict=True)

    def test_progress_quality_checks_the_slowest_validator(self):
        hosts = [Host(i, f"i-{i}", "public", f"10.0.0.{i + 1}") for i in range(3)]
        cfg = RunConfig(nodes=3, rate=99, image="image", metrics_port=6003)
        first = {host.index: "committed_transactions 0\n" for host in hosts}
        final_counts = [1_000, 1_000, 10]
        second = {
            host.index: f"committed_transactions {final_counts[host.index]}\n"
            for host in hosts
        }
        with patch("wanbench.collect.time.sleep"), \
             patch("wanbench.collect.time.monotonic", side_effect=[0, 10]), \
             patch("wanbench.collect._scrape_with_retry", side_effect=[first, second]):
            with self.assertRaisesRegex(RuntimeError, r"node\(s\) \[2\].*slowest 1 tx/s"):
                check_progress_quality(None, cfg, None, hosts)

    def test_progress_quality_can_report_high_load_congestion(self):
        hosts = [Host(i, f"i-{i}", "public", f"10.0.0.{i + 1}") for i in range(3)]
        cfg = RunConfig(nodes=3, rate=99, image="image", metrics_port=6003)
        first = {host.index: "committed_transactions 0\n" for host in hosts}
        final_counts = [1_000, 1_000, 10]
        second = {
            host.index: f"committed_transactions {final_counts[host.index]}\n"
            for host in hosts
        }
        with patch("wanbench.collect.time.sleep"), \
             patch("wanbench.collect.time.monotonic", side_effect=[0, 10]), \
             patch("wanbench.collect._scrape_with_retry", side_effect=[first, second]):
            check_progress_quality(
                None, cfg, None, hosts, enforce_rate_and_lag=False)

    def test_progress_quality_rejects_wide_boot_spread(self):
        hosts = [Host(i, f"i-{i}", "public", f"10.0.0.{i + 1}") for i in range(3)]
        cfg = RunConfig(nodes=3, rate=99, image="image", metrics_port=6003)
        first = {host.index: "committed_transactions 0\n" for host in hosts}
        second = {
            host.index: (
                "committed_transactions 1000\n"
                f"process_start_time_seconds {1_000 + host.index * 2}\n"
            )
            for host in hosts
        }
        with patch("wanbench.collect.time.sleep"), \
             patch("wanbench.collect.time.monotonic", side_effect=[0, 10]), \
             patch("wanbench.collect._scrape_with_retry", side_effect=[first, second]):
            with self.assertRaisesRegex(RuntimeError, r"boot spread 4\.0s"):
                check_progress_quality(None, cfg, None, hosts)

    def test_progress_quality_rejects_short_client_lead(self):
        hosts = [Host(i, f"i-{i}", "public", f"10.0.0.{i + 1}") for i in range(3)]
        cfg = RunConfig(nodes=3, rate=99, image="image", metrics_port=6003,
                        client_activate_at_ms=1_014_000)
        first = {host.index: "committed_transactions 0\n" for host in hosts}
        second = {
            host.index: (
                "committed_transactions 1000\n"
                f"process_start_time_seconds {1_003 + host.index}\n"
            )
            for host in hosts
        }
        with patch("wanbench.collect.time.sleep"), \
             patch("wanbench.collect.time.monotonic", side_effect=[0, 10]), \
             patch("wanbench.collect._scrape_with_retry", side_effect=[first, second]):
            with self.assertRaisesRegex(RuntimeError, r"only 9\.0s"):
                check_progress_quality(None, cfg, None, hosts)

    def test_starfish_metrics_port_depends_on_node_and_committee_size(self):
        cfg = RunConfig(protocol="starfish", nodes=50, rate=100, image="image")
        node = Host(7, "i-7", "public", "10.0.0.8")
        self.assertEqual(_metrics_ports(cfg, node), [1557])

    def test_transient_scrape_fault_recovers_within_retry_budget(self):
        hosts = [Host(0, "i-0", "public", "10.0.0.1"), Host(1, "i-1", "public", "10.0.0.2")]
        cfg = RunConfig(nodes=2, rate=100, image="image", metrics_port=6003)
        baseline_partial = {
            0: metrics(6007, 10, 1) + metrics(6003, 0, 2),
            1: "# WANBENCH_FAILED 6007\n# WANBENCH_FAILED 6003\n",
        }
        baseline_retry = {1: metrics(6007, 20, 1) + metrics(6003, 0, 2)}
        final_clean = {
            0: metrics(6007, 1010, 3) + metrics(6003, 0, 4),
            1: metrics(6007, 1020, 3) + metrics(6003, 0, 4),
        }
        calls = []

        def fake_scrape_all(_ssh, _cfg, _control, hs):
            calls.append(sorted(h.index for h in hs))
            return [baseline_partial, baseline_retry, final_clean][len(calls) - 1]

        with tempfile.TemporaryDirectory() as tmp, \
             patch("wanbench.collect.time.sleep"), \
             patch("wanbench.collect.time.monotonic", side_effect=[0, 0, 0, 10]), \
             patch("wanbench.collect.scrape_all", side_effect=fake_scrape_all):
            result = collect(None, cfg, None, hosts, tmp,
                             baseline_at=0, final_at=10, strict=True)
        self.assertEqual(calls, [[0, 1], [1], [0, 1]])
        self.assertEqual(result["tps_median"], 100.0)

    def test_persistent_scrape_fault_gives_up_after_bounded_retries(self):
        attempts = []

        def fake_scrape_all(_ssh, _cfg, _control, hs):
            attempts.append(sorted(h.index for h in hs))
            return {0: "# WANBENCH_FAILED 6007\n# WANBENCH_OK 6003\n"}

        with tempfile.TemporaryDirectory() as tmp, \
             patch("wanbench.collect.time.sleep"), \
             patch("wanbench.collect.scrape_all", side_effect=fake_scrape_all):
            with self.assertRaisesRegex(RuntimeError, "6007"):
                collect(None, self.cfg, None, self.hosts, tmp,
                        baseline_at=0, final_at=10, strict=True)
        self.assertEqual(len(attempts), 3)

class ScrapeOneFaultTests(unittest.TestCase):
    def setUp(self):
        self.cfg = RunConfig(nodes=1, rate=100, image="image", metrics_port=6003)
        self.node = Host(0, "i-0", "public", "10.0.0.1")

    def test_ssh_timeout_yields_failure_markers_instead_of_raising(self):
        ssh = MagicMock()
        ssh.run.side_effect = subprocess.TimeoutExpired(cmd="ssh", timeout=1)
        text = _scrape_one(ssh, self.cfg, None, self.node)
        self.assertEqual(_successful_ports(text), set())
        self.assertIn("# WANBENCH_FAILED 6007", text)
        self.assertIn("# WANBENCH_FAILED 6003", text)

    def test_ssh_oserror_yields_failure_markers_instead_of_raising(self):
        ssh = MagicMock()
        ssh.run.side_effect = OSError("connection reset by peer")
        text = _scrape_one(ssh, self.cfg, None, self.node)
        self.assertEqual(_successful_ports(text), set())

    def test_pool_map_never_sees_an_exception_from_one_bad_host(self):
        ssh = MagicMock()
        ssh.run.side_effect = subprocess.TimeoutExpired(cmd="ssh", timeout=1)
        snapshot = scrape_all(ssh, self.cfg, None, [self.node])
        self.assertEqual(_successful_ports(snapshot[0]), set())

    def test_ssh_timeout_widened_beyond_sum_of_per_port_curl_budgets(self):
        ssh = MagicMock(run=MagicMock(return_value="ok"))
        _scrape_one(ssh, self.cfg, None, self.node)
        _, kwargs = ssh.run.call_args
        self.assertGreater(kwargs["timeout"], 30 + 10 * 2 - 1)

if __name__ == "__main__":
    unittest.main()

class WorkerHealthFieldTests(unittest.TestCase):
    """Tests for per-node worker health reductions."""

    def setUp(self):
        self.cfg = RunConfig(nodes=2, rate=100, image="image", protocol="vantage")

    WORKER_PORT = 6007
    PRIMARY_PORT = 6003

    @classmethod
    def _scrape(cls, sync_peak, store_peak, age, panics, drained=0, primary_drained=0):
        return (
            f"# WANBENCH_OK {cls.WORKER_PORT}\n"
            f'worker_queue_peak{{queue="synchronizer"}} {sync_peak}\n'
            f'worker_queue_peak{{queue="store"}} {store_peak}\n'
            f"store_actor_heartbeat_age_ms {age}\n"
            f"store_commands_drained_total {drained}\n"
            f"process_panics {panics}\n"
            f"# WANBENCH_OK {cls.PRIMARY_PORT}\n"
            f"store_commands_drained_total {primary_drained}\n"
        )

    def _fields(self, fin, base=None, indices=None, window=60.0):
        return _worker_health_fields(
            self.cfg, fin, base if base is not None else {i: "" for i in fin},
            indices if indices is not None else sorted(fin), window)

    def test_one_wedged_node_survives_the_committee_reduction(self):
        fin = {
            0: self._scrape(2, 0, 12, 0),      # healthy
            1: self._scrape(1000, 100, 41_000, 1),  # wedged
        }
        got = self._fields(fin)
        self.assertEqual(got["worker_queue_peak_max_by_stage"],
                         {"synchronizer": 1000.0, "store": 100.0})
        self.assertEqual(got["store_heartbeat_age_ms_max"], 41_000)
        self.assertEqual(got["panics_total"], 1)

    def test_stages_are_ordered_worst_first(self):
        fin = {0: self._scrape(3, 97, 10, 0)}
        stages = list(self._fields(fin)["worker_queue_peak_max_by_stage"])
        self.assertEqual(stages, ["store", "synchronizer"])

    def test_heartbeat_age_is_maxed_not_summed_across_the_two_processes(self):
        fin = {0: "store_actor_heartbeat_age_ms 20\nstore_actor_heartbeat_age_ms 30\n"}
        got = self._fields(fin)
        self.assertEqual(got["store_heartbeat_age_ms_max"], 30)

    def test_older_images_report_zeros_rather_than_failing(self):
        got = self._fields({0: "committed_transactions 5\n"})
        self.assertEqual(got["worker_queue_peak_max_by_stage"], {})
        self.assertEqual(got["store_heartbeat_age_ms_max"], 0.0)
        self.assertEqual(got["panics_total"], 0)

    def test_absent_for_a_foreign_codebase(self):
        cfg = RunConfig(nodes=1, rate=100, image="image", protocol="starfish")
        self.assertEqual(
            _worker_health_fields(cfg, {0: self._scrape(1, 1, 1, 1)}, {0: ""}, [0], 60.0),
            {})

    def test_present_for_the_other_protocols_sharing_the_worker(self):
        for protocol in ("autobahn-seamless", "autobahn-optimistic",
                         "simple-it", "simple-it-bracha"):
            cfg = RunConfig(nodes=1, rate=100, image="image", protocol=protocol)
            got = _worker_health_fields(cfg, {0: self._scrape(7, 0, 5, 0)}, {0: ''}, [0], 60.0)
            self.assertEqual(got["worker_queue_peak_max_by_stage"],
                             {"synchronizer": 7.0, "store": 0.0},
                             f"{protocol} shares the worker and must be instrumented")


class StarfishMemoryFieldTests(unittest.TestCase):
    def test_reduces_committee_memory_metrics(self):
        cfg = RunConfig(nodes=2, rate=200, image="image", protocol="starfish")
        fin = {
            0: ("dag_blocks_in_memory 100\n"
                "global_in_memory_blocks_bytes 2000000\n"
                "dag_state_unloaded_blocks 40\n"),
            1: ("dag_blocks_in_memory 102\n"
                "global_in_memory_blocks_bytes 4000000\n"
                "dag_state_unloaded_blocks 60\n"),
        }

        self.assertEqual(
            _starfish_memory_fields(cfg, fin, [0, 1]),
            {
                "dag_blocks_in_memory_p50": 101.0,
                "dag_serialized_mb_in_memory_p50": 3.0,
                "dag_serialized_mb_in_memory_max": 4.0,
                "dag_state_unloaded_blocks_p50": 50.0,
            },
        )

    def test_is_absent_for_other_protocols(self):
        cfg = RunConfig(nodes=1, rate=100, image="image", protocol="vantage")
        self.assertEqual(_starfish_memory_fields(cfg, {0: ""}, [0]), {})

class MetricsWindowTimingTests(unittest.TestCase):
    """Tests for client activation and metrics-window timing."""

    def test_run_cmd_passes_the_client_instant_not_the_window(self):
        from wanbench.protocols import Vantage
        cfg = RunConfig(nodes=1, rate=100, image="image", protocol="vantage")
        cfg.client_activate_at_ms = 1_700_000_000_000
        cfg.metrics_active_at_ms = cfg.client_activate_at_ms + 10_000
        cmd = Vantage(cfg).run_cmd(Host(0, "i-0", "pub", "10.0.0.1"),
                                   [Host(0, "i-0", "pub", "10.0.0.1")])
        self.assertIn(f"ACTIVATE_AT_MS={cfg.client_activate_at_ms}", cmd)
        self.assertNotIn(f"ACTIVATE_AT_MS={cfg.metrics_active_at_ms}", cmd)

    def test_no_window_leaves_the_command_unchanged(self):
        from wanbench.protocols import Vantage
        cfg = RunConfig(nodes=1, rate=100, image="image", protocol="vantage")
        cmd = Vantage(cfg).run_cmd(Host(0, "i-0", "pub", "10.0.0.1"),
                                   [Host(0, "i-0", "pub", "10.0.0.1")])
        self.assertNotIn("ACTIVATE_AT_MS", cmd)

    def test_guard_waits_only_when_the_baseline_would_be_early(self):
        now = 1_700_000_000_000
        self.assertAlmostEqual(_wait_for_window_s(now, now + 8_000), 10.0)
        self.assertEqual(_wait_for_window_s(now, now - 1), 0.0)
        self.assertEqual(_wait_for_window_s(now, now), 0.0)
        self.assertEqual(_wait_for_window_s(now, None), 0.0)

class StoreDrainReductionTests(unittest.TestCase):
    """Tests for minimum per-worker store drain rate."""

    WORKER, PRIMARY = 6007, 6003

    def setUp(self):
        self.cfg = RunConfig(nodes=2, rate=200, image="image", protocol="vantage")

    def _node(self, worker_drained, primary_drained):
        return (f"# WANBENCH_OK {self.WORKER}\n"
                f"store_commands_drained_total {worker_drained}\n"
                f"# WANBENCH_OK {self.PRIMARY}\n"
                f"store_commands_drained_total {primary_drained}\n")

    def test_wedged_worker_is_not_masked_by_its_healthy_primary(self):
        base = {0: self._node(0, 0), 1: self._node(0, 0)}
        fin = {0: self._node(12_000, 6_000), 1: self._node(0, 6_000)}
        got = _worker_health_fields(self.cfg, fin, base, [0, 1], 60.0)
        self.assertEqual(got["worker_store_drained_per_s_min"], 0.0,
                         "a worker draining nothing must show as 0/s")
        self.assertEqual(got["worker_store_drained_per_s_median"], 100.0)

    def test_a_healthy_committee_reports_a_nonzero_minimum(self):
        base = {0: self._node(0, 0), 1: self._node(0, 0)}
        fin = {0: self._node(12_000, 600), 1: self._node(6_000, 600)}
        got = _worker_health_fields(self.cfg, fin, base, [0, 1], 60.0)
        self.assertEqual(got["worker_store_drained_per_s_min"], 100.0)

    def test_older_images_without_the_counter_report_zero_not_a_crash(self):
        fin = {0: f"# WANBENCH_OK {self.WORKER}\ncommitted_transactions 5\n"}
        got = _worker_health_fields(self.cfg, fin, {0: ""}, [0], 60.0)
        self.assertEqual(got["worker_store_drained_per_s_min"], 0.0)
        self.assertEqual(got["pending_payload_keys_max"], 0.0)

    def test_split_by_port_separates_the_two_processes(self):
        got = _split_by_port(self._node(7, 9))
        self.assertEqual(set(got), {self.WORKER, self.PRIMARY})
        self.assertIn("store_commands_drained_total 7", got[self.WORKER])
        self.assertIn("store_commands_drained_total 9", got[self.PRIMARY])

    def test_split_by_port_keeps_a_failed_port_section_empty(self):
        got = _split_by_port(f"# WANBENCH_FAILED {self.WORKER}\n"
                             f"# WANBENCH_OK {self.PRIMARY}\nfoo 1\n")
        self.assertEqual(got[self.WORKER].strip(), "")
        self.assertIn("foo 1", got[self.PRIMARY])

class WindowGuardTests(unittest.TestCase):
    """Tests that gate waits preserve the requested measurement duration."""

    def setUp(self):
        self.cfg = RunConfig(nodes=1, rate=100, image="image", metrics_port=6003)
        self.hosts = [Host(0, "i-0", "public", "10.0.0.1")]

    def test_guard_preserves_the_requested_gap_between_scrapes(self):
        base = {0: metrics(6007, 10, 1) + metrics(6003, 0, 2)}
        final = {0: metrics(6007, 1010, 3) + metrics(6003, 0, 4)}
        slept = []
        self.cfg.metrics_active_at_ms = int(1_700_000_000 * 1000) + 18_000
        with tempfile.TemporaryDirectory() as tmp, \
             patch("wanbench.collect.time.time", return_value=1_700_000_000.0), \
             patch("wanbench.collect.time.sleep", side_effect=slept.append), \
             patch("wanbench.collect.time.monotonic", side_effect=[0, 0, 0, 100]), \
             patch("wanbench.collect.scrape_all", side_effect=[base, final]):
            collect(None, self.cfg, None, self.hosts, tmp,
                    baseline_at=10, final_at=70)
        self.assertEqual(slept[0], 10, "baseline_at honoured")
        self.assertAlmostEqual(slept[1], 20.0, msg="guard waits gate + margin")
        self.assertAlmostEqual(slept[2], 90.0, msg="final pushed out by the guard's wait")

    def test_no_guard_wait_leaves_timing_untouched(self):
        base = {0: metrics(6007, 10, 1) + metrics(6003, 0, 2)}
        final = {0: metrics(6007, 1010, 3) + metrics(6003, 0, 4)}
        slept = []
        with tempfile.TemporaryDirectory() as tmp, \
             patch("wanbench.collect.time.sleep", side_effect=slept.append), \
             patch("wanbench.collect.time.monotonic", side_effect=[0, 0, 0, 100]), \
             patch("wanbench.collect.scrape_all", side_effect=[base, final]):
            collect(None, self.cfg, None, self.hosts, tmp,
                    baseline_at=10, final_at=70)
        self.assertEqual(slept[0], 10)
        self.assertAlmostEqual(slept[1], 70.0, msg="ungated run keeps its original timing")

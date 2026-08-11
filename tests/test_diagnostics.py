import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from wanbench.diagnostics import _tcp_summary, capture_nodes
from wanbench.ssh import Host


class DiagnosticsTests(unittest.TestCase):
    def test_capture_is_bounded_per_node_and_reports_partial_errors(self):
        hosts = [
            Host(i, f"i-{i}", f"public-{i}", f"10.0.0.{i + 1}")
            for i in range(2)
        ]
        ssh = MagicMock()
        ssh.parallel.side_effect = lambda items, action, max_workers: [
            action(host) for host in items
        ]
        ssh.run.side_effect = [
            "[docker-state]\nok\n"
            "[primary-tcp]\n"
            "0 0 10.0.0.1:6001 10.0.0.2:50000\n"
            " cubic rtt:12.5/1.0\n",
            RuntimeError("offline"),
        ]

        with tempfile.TemporaryDirectory() as tmp:
            complete = capture_nodes(ssh, hosts, tmp, "quality")
            first = Path(tmp, "quality-diagnostics-node-0.txt").read_text()
            second = Path(tmp, "quality-diagnostics-node-1.txt").read_text()
            network = Path(tmp, "quality-network-rtt.json").read_text()

        self.assertEqual(complete, 1)
        self.assertIn("[docker-state]", first)
        self.assertIn("[capture-error]", second)
        self.assertIn('"mean_rtt_ms": 12.5', network)
        for call in ssh.run.call_args_list:
            self.assertEqual(call.kwargs["timeout"], 90)
            self.assertFalse(call.kwargs["check"])

    def test_capture_rejects_unsafe_tag(self):
        with self.assertRaisesRegex(ValueError, "invalid diagnostic tag"):
            capture_nodes(MagicMock(), [], ".", "../../bad")

    def test_tcp_summary_counts_sessions_peers_and_mean_rtt(self):
        text = (
            "[primary-tcp]\n"
            "0 0 10.0.0.1:6001 10.0.0.2:50000\n"
            " cubic rtt:10.0/1.0\n"
            "0 0 10.0.0.1:50001 10.0.0.2:6001\n"
            " cubic rtt:14.0/1.0\n"
            "0 0 10.0.0.1:6001 10.0.0.3:50002\n"
            " cubic rtt:18.0/1.0\n"
            "[worker-tcp]\n"
        )
        self.assertEqual(
            _tcp_summary(text, "primary-tcp"),
            {
                "mean_rtt_ms": 14.0,
                "rtt_samples": 3,
                "sessions": 3,
                "peer_ips": 2,
            },
        )


if __name__ == "__main__":
    unittest.main()

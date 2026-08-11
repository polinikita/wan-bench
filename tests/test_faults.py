import unittest
from unittest.mock import MagicMock

from wanbench.faults import _install, ring
from wanbench.config import RunConfig
from wanbench.ssh import Host

class FaultTests(unittest.TestCase):
    def test_vantage_link_cut_excludes_transaction_submission_port(self):
        ssh = MagicMock()
        host = Host(0, "i-0", "public", "10.0.0.1")
        peer = Host(1, "i-1", "public", "10.0.0.2")
        cfg = RunConfig(nodes=2, rate=100, image="image")
        _install(ssh, cfg, host, [peer], "cut")
        command = ssh.sudo.call_args.args[1]
        self.assertIn("--dport 6000", command)
        self.assertIn("--dport 6001", command)
        self.assertIn("--dport 6006", command)
        self.assertNotIn("--dport 6005", command)

    def test_starfish_link_cut_uses_target_network_port(self):
        ssh = MagicMock()
        host = Host(0, "i-0", "public", "10.0.0.1")
        peer = Host(3, "i-3", "public", "10.0.0.4")
        cfg = RunConfig(protocol="starfish", nodes=4, rate=100, image="image")
        _install(ssh, cfg, host, [peer], "cut")
        self.assertIn("--dport 1503", ssh.sudo.call_args.args[1])

    def test_ring_rejects_invalid_percentage(self):
        ssh = MagicMock()
        hosts = [Host(i, f"i-{i}", "public", f"10.0.0.{i + 1}") for i in range(2)]
        with self.assertRaisesRegex(ValueError, "between 1 and 100"):
            ring(ssh, RunConfig(nodes=2, rate=100, image="image"), hosts, 0)

if __name__ == "__main__":
    unittest.main()

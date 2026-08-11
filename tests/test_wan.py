"""Tests for tc rule generation and verification."""
import unittest
from unittest.mock import MagicMock, patch

from wanbench import prepare
from wanbench.config import RunConfig
from wanbench.ssh import Host

def hosts(n):
    return [Host(i, f"i-{i}", f"pub{i}", f"10.0.0.{i + 1}") for i in range(n)]

def cfg_netem(**kw):
    c = RunConfig(nodes=kw.pop("nodes", 4), rate=1000, image="img", **kw)
    c.wan.mode = "netem"
    return c

class TcScriptTests(unittest.TestCase):
    def test_every_peer_gets_a_class_a_netem_qdisc_and_a_hashed_filter(self):
        c = cfg_netem()
        script = prepare.tc_script(c, hosts(4), me=0)
        for peer in (1, 2, 3):
            self.assertIn(f"classid 1:{100 + peer}", script)
            self.assertIn(f"handle {100 + peer}: netem", script)
        self.assertNotIn("classid 1:100 ", script, "no class for ourselves")

    def test_netem_limit_is_explicit_and_precedes_delay(self):
        c = cfg_netem()
        c.wan.netem_limit_pkts = 54321
        script = prepare.tc_script(c, hosts(3), me=0)
        self.assertIn("netem limit 54321 delay", script)

    def test_classification_is_hashed_not_a_flat_filter_list(self):
        c = cfg_netem()
        script = prepare.tc_script(c, hosts(4), me=0)
        self.assertIn("u32 divisor 256", script)
        self.assertIn("hashkey mask 0x000000ff at 16", script)
        self.assertIn("ht 2:2: match ip dst 10.0.0.2/32", script)

    def test_a_last_octet_above_15_uses_a_hex_bucket(self):
        c = cfg_netem(nodes=2)
        h = [Host(0, "i-0", "p0", "10.0.0.1"), Host(1, "i-1", "p1", "10.0.0.32")]
        script = prepare.tc_script(c, h, me=0)
        self.assertIn("ht 2:20: match ip dst 10.0.0.32/32", script)

class VerifyPairsTests(unittest.TestCase):
    def test_pairs_span_the_expected_rtt_range(self):
        c = cfg_netem(nodes=10)
        pairs = prepare.verify_pairs(c, hosts(10), count=4)
        self.assertEqual(len(pairs), 4)
        rtts = [prepare.one_way_ms(c, i, j) for i, j in pairs]
        self.assertEqual(rtts, sorted(rtts))
        allr = [prepare.one_way_ms(c, i, j)
                for i in range(10) for j in range(10) if i < j]
        self.assertAlmostEqual(rtts[0], min(allr), places=6)
        self.assertAlmostEqual(rtts[-1], max(allr), places=6)

    def test_fewer_hosts_than_samples_returns_every_pair(self):
        c = cfg_netem(nodes=3)
        self.assertEqual(len(prepare.verify_pairs(c, hosts(3), count=10)), 3)

    def test_a_single_host_has_nothing_to_verify(self):
        self.assertEqual(prepare.verify_pairs(cfg_netem(nodes=1), hosts(1)), [])

class VerifyNetemTests(unittest.TestCase):
    def _ssh(self, avg_for_pair):
        ssh = MagicMock()
        ssh.run.side_effect = lambda host, cmd, **kw: (
            f"rtt min/avg/max/mdev = 1.0/{avg_for_pair(host.index, cmd)}/9.9/0.1 ms")
        ssh.fanout.return_value = ["0"]
        return ssh

    def test_measured_rtt_matching_expectation_passes(self):
        c = cfg_netem(nodes=4)
        hs = hosts(4)
        expected = {(i, j): prepare.one_way_ms(c, i, j) * 2
                    for i, j in prepare.verify_pairs(c, hs)}

        def avg(src, cmd):
            for (i, j), rtt in expected.items():
                if i == src and f"10.0.0.{j + 1}" in cmd:
                    return f"{rtt:.3f}"
            return "0.1"

        prepare.verify_netem(self._ssh(avg), c, hs)  # must not raise

    def test_a_pair_measuring_lan_speed_is_fatal(self):
        c = cfg_netem(nodes=4)
        hs = hosts(4)
        pairs = prepare.verify_pairs(c, hs)
        victim = pairs[-1]  # the largest expected RTT

        def avg(src, cmd):
            if src == victim[0] and f"10.0.0.{victim[1] + 1}" in cmd:
                return "0.180"  # unshaped LAN
            for i, j in pairs:
                if i == src and f"10.0.0.{j + 1}" in cmd:
                    return f"{prepare.one_way_ms(c, i, j) * 2:.3f}"
            return "0.1"

        with self.assertRaisesRegex(RuntimeError, "shaping is missing or mis-filtered"):
            prepare.verify_netem(self._ssh(avg), c, hs)

    def test_an_rtt_above_expectation_only_warns(self):
        c = cfg_netem(nodes=4)
        hs = hosts(4)

        def avg(src, cmd):
            for i, j in prepare.verify_pairs(c, hs):
                if i == src and f"10.0.0.{j + 1}" in cmd:
                    return f"{prepare.one_way_ms(c, i, j) * 2 * 3 + 50:.3f}"
            return "0.1"

        prepare.verify_netem(self._ssh(avg), c, hs)  # warns, must not raise

    def test_unparseable_ping_output_is_fatal(self):
        c = cfg_netem(nodes=4)
        ssh = MagicMock()
        ssh.run.return_value = "ping: connect: Network is unreachable"
        ssh.fanout.return_value = ["0"]
        with self.assertRaisesRegex(RuntimeError, "no RTT in ping output"):
            prepare.verify_netem(ssh, c, hosts(4))

    def test_mimic_mode_applies_no_tc_and_verifies_nothing(self):
        c = RunConfig(nodes=4, rate=1000, image="img")
        c.wan.mode = "mimic"
        ssh = MagicMock()
        prepare.apply_wan(ssh, c, hosts(4))
        ssh.fanout.assert_not_called()
        ssh.run.assert_not_called()

class NetemDropTests(unittest.TestCase):
    @staticmethod
    def _tc(*drops):
        blocks = []
        for i, dropped in enumerate(drops, start=10):
            blocks.append(
                f"qdisc netem {i}: parent 1:{i} limit 100000 delay 50.0ms\n"
                f" Sent 12345 bytes 100 pkt (dropped {dropped}, overlimits 0 requeues 0)\n"
                " backlog 0b 0p requeues 0\n"
            )
        return "".join(blocks)

    def test_parser_handles_tc_parenthesized_dropped_token(self):
        text = (
            "qdisc htb 1: root refcnt 2 r2q 10 default 0x3e7 direct_packets_stat 0\n"
            " Sent 1 bytes 1 pkt (dropped 999, overlimits 0 requeues 0)\n"
            + self._tc(12, 0, 30)
        )
        self.assertEqual(prepare.parse_netem_drops(text), (42, 3, 3))

    def test_drops_are_summed_across_the_fleet(self):
        ssh = MagicMock()
        ssh.fanout.return_value = [self._tc(12, 0), self._tc(30, 0), self._tc(0, 0)]
        with patch("builtins.print") as p:
            total = prepare.report_netem_drops(ssh, hosts(3))
        self.assertEqual(total, 42)
        said = " ".join(str(c) for c in p.call_args_list)
        self.assertIn("42", said)
        self.assertIn("NON-ZERO", said)

    def test_zero_drops_reports_without_the_warning(self):
        ssh = MagicMock()
        ssh.fanout.return_value = [self._tc(0, 0), self._tc(0, 0), self._tc(0, 0)]
        with patch("builtins.print") as p:
            prepare.report_netem_drops(ssh, hosts(3))
        said = " ".join(str(c) for c in p.call_args_list)
        self.assertIn("across the fleet: 0", said)
        self.assertNotIn("NON-ZERO", said)

    def test_partial_qdisc_output_never_claims_zero_drops(self):
        ssh = MagicMock()
        ssh.fanout.return_value = [self._tc(0, 0), self._tc(0), self._tc(0, 0)]
        with patch("builtins.print") as p:
            total = prepare.report_netem_drops(ssh, hosts(3))
        self.assertIsNone(total)
        self.assertIn("UNKNOWN", " ".join(str(c) for c in p.call_args_list))

    def test_unparseable_output_never_claims_zero_drops(self):
        ssh = MagicMock()
        ssh.fanout.return_value = ["", "tc: permission denied"]
        with patch("builtins.print") as p:
            total = prepare.report_netem_drops(ssh, hosts(2))
        self.assertIsNone(total)
        said = " ".join(str(c) for c in p.call_args_list)
        self.assertIn("UNKNOWN", said)
        self.assertNotIn("across the fleet: 0", said)

    def test_a_broken_counter_read_never_sinks_the_run(self):
        ssh = MagicMock()
        ssh.fanout.side_effect = RuntimeError("ssh died")
        prepare.report_netem_drops(ssh, hosts(2))  # must not raise

if __name__ == "__main__":
    unittest.main()

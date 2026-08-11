import unittest
from unittest.mock import patch

from wanbench.ssh import Host, Ssh


class ParallelTests(unittest.TestCase):
    def test_parallel_runs_each_action_once_and_preserves_order(self):
        hosts = [
            Host(i, f"i-{i}", f"public-{i}", f"10.0.0.{i + 1}")
            for i in range(4)
        ]
        ssh = Ssh("key", "ubuntu")
        seen = []

        result = ssh.parallel(hosts, lambda host: seen.append(host.index) or host.index)

        self.assertEqual(result, [0, 1, 2, 3])
        self.assertEqual(sorted(seen), [0, 1, 2, 3])

    @patch("wanbench.ssh.subprocess.run")
    def test_fetch_copies_from_the_remote_host(self, run):
        run.return_value.returncode = 0
        ssh = Ssh("key", "ubuntu")
        host = Host(3, "i-3", "public-3", "10.0.0.4")

        ssh.fetch(host, "/tmp/data.tar.gz", "/local/data.tar.gz", timeout=45)

        args = run.call_args.args[0]
        self.assertIn("ubuntu@public-3:/tmp/data.tar.gz", args)
        self.assertEqual(args[-1], "/local/data.tar.gz")
        self.assertEqual(run.call_args.kwargs["timeout"], 45)


if __name__ == "__main__":
    unittest.main()

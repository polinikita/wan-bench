import unittest
from unittest.mock import MagicMock, patch

from wanbench.aws import Aws
from wanbench.config import RunConfig
from wanbench.ssh import Host
import wanbench.run as run_mod

class LifecycleTests(unittest.TestCase):
    def test_up_failure_terminates_every_instance(self):
        cfg = RunConfig(nodes=1, rate=100, image="image")
        node = Host(0, "i-node", "public-node", "10.0.0.1")
        control = Host(1, "i-control", "public-control", "10.0.0.2")
        aws = MagicMock()
        aws.provision.return_value = [node]
        aws.control_host.return_value = control
        ssh = MagicMock()
        ssh.wait_ready.side_effect = RuntimeError("ssh unavailable")
        with patch.object(run_mod, "Aws", return_value=aws), \
             patch.object(run_mod, "Ssh", return_value=ssh), \
             patch.object(run_mod.monitoring, "_my_ip", return_value="203.0.113.1/32"), \
             patch.object(run_mod.images, "pin_to_digest",
                          side_effect=lambda ref: (ref, None)):
            with self.assertRaisesRegex(RuntimeError, "ssh unavailable"):
                run_mod.up(cfg)
        aws.terminate.assert_called_once_with(keep_control=False)
        aws._wait_terminated.assert_called_once_with(role=None)
        aws.delete_security_group.assert_called_once_with()
        self.assertEqual(cfg.ssh_open_cidr, "203.0.113.1/32")

    def test_full_down_waits_and_deletes_the_security_group(self):
        cfg = RunConfig(nodes=1, rate=100, image="image")
        aws = MagicMock()
        aws.terminate.return_value = ["i-node", "i-control"]
        aws.delete_security_group.return_value = ["sg-run"]
        with patch.object(run_mod, "Aws", return_value=aws):
            run_mod.down(cfg, keep_monitoring=False)
        aws.terminate.assert_called_once_with(keep_control=False)
        aws._wait_terminated.assert_called_once_with(role=None)
        aws.delete_security_group.assert_called_once_with()

    def test_kept_monitoring_prevents_security_group_deletion(self):
        cfg = RunConfig(nodes=1, rate=100, image="image")
        aws = MagicMock()
        aws.control_host.return_value = Host(
            1, "i-control", "public-control", "10.0.0.2")
        with patch.object(run_mod, "Aws", return_value=aws):
            run_mod.down(cfg, keep_monitoring=True)
        aws.terminate.assert_called_once_with(keep_control=True)
        aws.delete_security_group.assert_not_called()

    def test_incomplete_tagged_fleet_is_replaced_before_provisioning(self):
        cfg = RunConfig(nodes=2, rate=100, image="image", instance_type="c5d.2xlarge")
        aws = Aws.__new__(Aws)
        aws.cfg = cfg
        stale = Host(0, "i-control", "public", "10.0.0.1")
        aws.describe = MagicMock(return_value=[stale])
        aws.terminate = MagicMock(return_value=[stale.instance_id])
        aws._wait_terminated = MagicMock()
        expected = [MagicMock(), MagicMock()]
        aws._provision_type = MagicMock(return_value=expected)

        self.assertIs(aws.provision(), expected)
        aws.terminate.assert_called_once_with(keep_control=True)
        aws._wait_terminated.assert_called_once_with()
        aws._provision_type.assert_called_once_with()

class ImagePinningTests(unittest.TestCase):
    """Tests for immutable image resolution."""

    def test_a_tag_is_rewritten_to_the_digest_the_registry_reports(self):
        import wanbench.images as images
        with patch.object(images, "verify_registry_manifest",
                          return_value="sha256:abc123"):
            ref, digest = images.pin_to_digest("ghcr.io/o/vantage-node:latest")
        self.assertEqual(ref, "ghcr.io/o/vantage-node@sha256:abc123")
        self.assertEqual(digest, "sha256:abc123")

    def test_an_already_pinned_ref_is_returned_unchanged_and_not_re_verified(self):
        import wanbench.images as images
        pinned = "ghcr.io/o/vantage-node@sha256:deadbeef"
        with patch.object(images, "verify_registry_manifest") as verify:
            ref, digest = images.pin_to_digest(pinned)
        verify.assert_not_called()
        self.assertEqual((ref, digest), (pinned, "sha256:deadbeef"))

    def test_a_registry_reporting_no_digest_degrades_to_the_tag(self):
        import wanbench.images as images
        with patch.object(images, "verify_registry_manifest", return_value=None):
            ref, digest = images.pin_to_digest("10.0.0.9:5000/vantage-node:dev")
        self.assertEqual((ref, digest), ("10.0.0.9:5000/vantage-node:dev", None))

class SingleAzTests(unittest.TestCase):
    """Tests for single-AZ fleet enforcement."""

    def _aws(self, cfg, live_azs):
        aws = Aws.__new__(Aws)
        aws.cfg = cfg
        aws._describe_instances = MagicMock(return_value=[
            {"Placement": {"AvailabilityZone": az}, "InstanceId": f"i-{n}"}
            for n, az in enumerate(live_azs)])
        return aws

    def test_fleet_az_refuses_a_fleet_already_spanning_two_zones(self):
        aws = self._aws(RunConfig(nodes=2, rate=100, image="i"),
                        ["eu-north-1a", "eu-north-1b"])
        with self.assertRaisesRegex(RuntimeError, "must live in ONE AZ"):
            aws._fleet_az()

    def test_a_pin_wins_when_no_instances_exist(self):
        cfg = RunConfig(nodes=2, rate=100, image="i", az="eu-north-1b")
        self.assertEqual(self._aws(cfg, [])._fleet_az(), "eu-north-1b")

    def test_a_pin_contradicting_a_retained_host_is_fatal(self):
        cfg = RunConfig(nodes=2, rate=100, image="i", az="eu-north-1b")
        aws = self._aws(cfg, ["eu-north-1a"])
        with self.assertRaisesRegex(RuntimeError, "pins az=eu-north-1b"):
            aws._fleet_az()

    def test_unpinned_adopts_the_existing_zone(self):
        cfg = RunConfig(nodes=2, rate=100, image="i")
        self.assertEqual(self._aws(cfg, ["eu-north-1c"])._fleet_az(), "eu-north-1c")

    def test_post_condition_rejects_a_mixed_provisioned_fleet(self):
        cfg = RunConfig(nodes=2, rate=100, image="i")
        aws = self._aws(cfg, ["eu-north-1a", "eu-north-1b"])
        with self.assertRaisesRegex(RuntimeError, "single-AZ rule was violated"):
            aws._assert_single_az([])

    def test_post_condition_rejects_the_wrong_zone_when_pinned(self):
        cfg = RunConfig(nodes=2, rate=100, image="i", az="eu-north-1b")
        aws = self._aws(cfg, ["eu-north-1a"])
        with self.assertRaisesRegex(RuntimeError, "pins az=eu-north-1b"):
            aws._assert_single_az([])

    def test_post_condition_passes_on_a_single_zone(self):
        cfg = RunConfig(nodes=2, rate=100, image="i", az="eu-north-1b")
        self._aws(cfg, ["eu-north-1b"])._assert_single_az([])  # must not raise

    def test_fleet_info_allows_a_different_control_instance_type(self):
        cfg = RunConfig(nodes=2, rate=100, image="i")
        aws = Aws.__new__(Aws)
        aws.cfg = cfg
        validators = [
            {
                "Placement": {"AvailabilityZone": "eu-north-1b"},
                "InstanceType": "m5d.2xlarge",
                "ImageId": "ami-validator",
            }
            for _ in range(2)
        ]
        control = {
            "Placement": {"AvailabilityZone": "eu-north-1b"},
            "InstanceType": "c5.large",
            "ImageId": "ami-control",
        }
        aws._describe_instances = MagicMock(
            side_effect=lambda _states, role=None: validators if role else [*validators, control])
        self.assertEqual(aws.fleet_info(), {
            "az": "eu-north-1b",
            "instance_type": "m5d.2xlarge",
            "ami": "ami-validator",
        })


class SecurityGroupTests(unittest.TestCase):
    def test_existing_group_replaces_stale_world_open_ssh_rule(self):
        cfg = RunConfig(nodes=2, rate=100, image="i",
                        ssh_open_cidr="203.0.113.8/32")
        aws = Aws.__new__(Aws)
        aws.cfg = cfg
        aws.ec2 = MagicMock()
        aws.ec2.describe_security_groups.return_value = {
            "SecurityGroups": [{
                "GroupId": "sg-run",
                "IpPermissions": [{
                    "IpProtocol": "tcp",
                    "FromPort": 22,
                    "ToPort": 22,
                    "IpRanges": [{"CidrIp": "0.0.0.0/0"}],
                }],
            }],
        }
        self.assertEqual(aws.ensure_security_group("vpc-default"), "sg-run")
        revoked = aws.ec2.revoke_security_group_ingress.call_args.kwargs
        self.assertEqual(revoked["IpPermissions"][0]["IpRanges"],
                         [{"CidrIp": "0.0.0.0/0"}])
        authorized = [
            call.kwargs["IpPermissions"][0]
            for call in aws.ec2.authorize_security_group_ingress.call_args_list
        ]
        self.assertIn({
            "IpProtocol": "tcp", "FromPort": 22, "ToPort": 22,
            "IpRanges": [{"CidrIp": "203.0.113.8/32"}],
        }, authorized)

    def test_open_port_replaces_existing_cidr(self):
        aws = Aws.__new__(Aws)
        aws.ec2 = MagicMock()
        aws.ec2.describe_security_groups.return_value = {
            "SecurityGroups": [{"IpPermissions": [{
                "IpProtocol": "tcp",
                "FromPort": 3000,
                "ToPort": 3000,
                "IpRanges": [{"CidrIp": "0.0.0.0/0"}],
            }]}],
        }

        aws._replace_port_ranges("sg-run", 3000, "203.0.113.8/32")

        revoked = aws.ec2.revoke_security_group_ingress.call_args.kwargs
        self.assertEqual(revoked["IpPermissions"][0]["IpRanges"],
                         [{"CidrIp": "0.0.0.0/0"}])
        authorized = aws.ec2.authorize_security_group_ingress.call_args.kwargs
        self.assertEqual(authorized["IpPermissions"][0]["IpRanges"],
                         [{"CidrIp": "203.0.113.8/32"}])

    def test_close_port_revokes_existing_cidrs(self):
        aws = Aws.__new__(Aws)
        aws.ec2 = MagicMock()
        aws.ec2.describe_security_groups.return_value = {
            "SecurityGroups": [{"IpPermissions": [{
                "IpProtocol": "tcp",
                "FromPort": 3000,
                "ToPort": 3000,
                "IpRanges": [
                    {"CidrIp": "0.0.0.0/0"},
                    {"CidrIp": "203.0.113.8/32"},
                ],
            }]}],
        }

        aws._replace_port_ranges("sg-run", 3000, None)

        revoked = aws.ec2.revoke_security_group_ingress.call_args.kwargs
        self.assertEqual(revoked["IpPermissions"][0]["IpRanges"], [
            {"CidrIp": "0.0.0.0/0"},
            {"CidrIp": "203.0.113.8/32"},
        ])
        aws.ec2.authorize_security_group_ingress.assert_not_called()

if __name__ == "__main__":
    unittest.main()

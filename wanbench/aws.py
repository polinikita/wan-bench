"""Tag-scoped EC2 provisioning for a single-AZ benchmark fleet."""

from __future__ import annotations

import base64
import re
import time

import boto3
from botocore.exceptions import ClientError

from .config import RunConfig
from .ssh import Host

_UBUNTU_SSM = "/aws/service/canonical/ubuntu/server/22.04/stable/current/amd64/hvm/ebs-gp2/ami-id"

# Docker is installed explicitly after SSH becomes available.
_USER_DATA = "#!/bin/bash\ntouch /var/lib/wan-bench-ready\n"


class Aws:
    def __init__(self, cfg: RunConfig):
        self.cfg = cfg
        self.session = boto3.Session(profile_name=cfg.profile, region_name=cfg.region)
        self.ec2 = self.session.client("ec2")
        self.ssm = self.session.client("ssm")
        self.sts = self.session.client("sts")
        self.quotas = self.session.client("service-quotas")

    # Canonical's AWS account for the SSM fallback.
    _CANONICAL = "099720109477"
    _UBUNTU_NAME = "ubuntu/images/hvm-ssd/ubuntu-jammy-22.04-amd64-server-*"

    def _ami(self) -> str:
        if self.cfg.ami:
            return self.cfg.ami
        try:
            return self.ssm.get_parameter(Name=_UBUNTU_SSM)["Parameter"]["Value"]
        except Exception:
            # Resolve through EC2 when SSM access is unavailable.
            imgs = self.ec2.describe_images(
                Owners=[self._CANONICAL],
                Filters=[{"Name": "name", "Values": [self._UBUNTU_NAME]},
                         {"Name": "state", "Values": ["available"]}],
            )["Images"]
            if not imgs:
                raise RuntimeError("no Ubuntu 22.04 AMI found; set config.ami")
            imgs.sort(key=lambda i: i["CreationDate"])
            return imgs[-1]["ImageId"]

    def _default_subnets(self) -> list[tuple[str, str, str]]:
        """Return default subnets as ``(subnet_id, vpc_id, az)`` tuples."""
        subs = self.ec2.describe_subnets(
            Filters=[{"Name": "default-for-az", "Values": ["true"]}]
        )["Subnets"]
        if not subs:
            raise RuntimeError(f"no default subnet in {self.cfg.region}; set one up first")
        subs.sort(key=lambda s: s["AvailabilityZone"])
        return [(s["SubnetId"], s["VpcId"], s["AvailabilityZone"]) for s in subs]

    def _default_subnet(self):
        s = self._default_subnets()[0]
        return s[0], s[1]

    def _tag_spec(self, kind: str, role: str | None = None) -> dict:
        tags = dict(self.cfg.tags)
        if role is not None:
            tags["Role"] = role
        return {
            "ResourceType": kind,
            "Tags": [{"Key": k, "Value": v} for k, v in tags.items()],
        }

    def ensure_security_group(self, vpc_id: str) -> str:
        if not self.cfg.ssh_open_cidr:
            raise RuntimeError("ssh_open_cidr must be resolved before provisioning")
        name = f"wan-bench-{self.cfg.run_id}"
        existing = self.ec2.describe_security_groups(
            Filters=[{"Name": "group-name", "Values": [name]},
                     {"Name": "vpc-id", "Values": [vpc_id]}]
        )["SecurityGroups"]
        if existing:
            gid = existing[0]["GroupId"]
            self.ec2.create_tags(
                Resources=[gid], Tags=self._tag_spec("security-group")["Tags"])
            self._configure_security_group(gid, existing[0])
            return gid
        gid = self.ec2.create_security_group(
            GroupName=name, Description="wan-bench run", VpcId=vpc_id,
            TagSpecifications=[self._tag_spec("security-group")],
        )["GroupId"]
        self._configure_security_group(gid, {"IpPermissions": []})
        return gid

    def _configure_security_group(self, gid: str, group: dict) -> None:
        desired = self.cfg.ssh_open_cidr
        for permission in group.get("IpPermissions", []):
            if (permission.get("IpProtocol") != "tcp" or
                    permission.get("FromPort") != 22 or
                    permission.get("ToPort") != 22):
                continue
            stale = [item for item in permission.get("IpRanges", [])
                     if item.get("CidrIp") != desired]
            if stale:
                self.ec2.revoke_security_group_ingress(
                    GroupId=gid,
                    IpPermissions=[{
                        "IpProtocol": "tcp", "FromPort": 22, "ToPort": 22,
                        "IpRanges": stale,
                    }],
                )

        permissions = [
            {"IpProtocol": "tcp", "FromPort": 22, "ToPort": 22,
             "IpRanges": [{"CidrIp": desired}]},
            {"IpProtocol": "-1", "UserIdGroupPairs": [{"GroupId": gid}]},
        ]
        for permission in permissions:
            try:
                self.ec2.authorize_security_group_ingress(
                    GroupId=gid, IpPermissions=[permission])
            except ClientError as exc:
                code = exc.response.get("Error", {}).get("Code")
                if code != "InvalidPermission.Duplicate":
                    raise

    def open_port(self, port: int, cidr: str) -> None:
        """Set the allowed CIDR for one TCP port."""
        _, vpc_id = self._default_subnet()
        gid = self.ensure_security_group(vpc_id)
        self._replace_port_ranges(gid, port, cidr)

    def close_port(self, port: int) -> None:
        """Remove every external CIDR from one TCP port."""
        _, vpc_id = self._default_subnet()
        gid = self.ensure_security_group(vpc_id)
        self._replace_port_ranges(gid, port, None)

    def _replace_port_ranges(self, gid: str, port: int,
                             cidr: str | None) -> None:
        group = self.ec2.describe_security_groups(GroupIds=[gid])["SecurityGroups"][0]
        for permission in group.get("IpPermissions", []):
            if (permission.get("IpProtocol") != "tcp" or
                    permission.get("FromPort") != port or
                    permission.get("ToPort") != port):
                continue
            stale = [item for item in permission.get("IpRanges", [])
                     if item.get("CidrIp") != cidr]
            if stale:
                self.ec2.revoke_security_group_ingress(
                    GroupId=gid,
                    IpPermissions=[{
                        "IpProtocol": "tcp", "FromPort": port, "ToPort": port,
                        "IpRanges": stale,
                    }],
                )
        if cidr is None:
            return
        try:
            self.ec2.authorize_security_group_ingress(
                GroupId=gid,
                IpPermissions=[{"IpProtocol": "tcp", "FromPort": port, "ToPort": port,
                                "IpRanges": [{"CidrIp": cidr}]}],
            )
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            if code != "InvalidPermission.Duplicate":
                raise

    def delete_security_group(self, timeout_s: int = 120) -> list[str]:
        """Delete this run's security groups after their interfaces are gone."""
        groups = self.ec2.describe_security_groups(Filters=[
            {"Name": "tag:Project", "Values": ["wan-bench"]},
            {"Name": "tag:Run", "Values": [self.cfg.run_id]},
        ])["SecurityGroups"]
        deleted: list[str] = []
        for group in groups:
            gid = group["GroupId"]
            deadline = time.monotonic() + timeout_s
            while True:
                try:
                    self.ec2.delete_security_group(GroupId=gid)
                    deleted.append(gid)
                    break
                except ClientError as exc:
                    code = exc.response.get("Error", {}).get("Code")
                    if code == "InvalidGroup.NotFound":
                        break
                    if code != "DependencyViolation" or time.monotonic() >= deadline:
                        raise
                    time.sleep(2)
        return deleted

    def preflight(self) -> dict:
        """Verify read-only AWS prerequisites for this configuration."""
        identity = self.sts.get_caller_identity()
        self.ec2.describe_key_pairs(KeyNames=[self.cfg.key_name])
        subnets = self._default_subnets()
        azs = [az for _subnet, _vpc, az in subnets]
        if self.cfg.az and self.cfg.az not in azs:
            raise RuntimeError(
                f"az {self.cfg.az} has no default subnet in {self.cfg.region}")
        quota: dict | None = None
        if self.cfg.instance_type:
            locations = [self.cfg.az] if self.cfg.az else azs
            offerings = self.ec2.describe_instance_type_offerings(
                LocationType="availability-zone",
                Filters=[
                    {"Name": "instance-type", "Values": [self.cfg.instance_type]},
                    {"Name": "location", "Values": locations},
                ],
            )["InstanceTypeOfferings"]
            if not offerings:
                raise RuntimeError(
                    f"{self.cfg.instance_type} is unavailable in {locations}")
            instance = self.ec2.describe_instance_types(
                InstanceTypes=[self.cfg.instance_type]
            )["InstanceTypes"][0]
            required_vcpus = instance["VCpuInfo"]["DefaultVCpus"] * (self.cfg.nodes + 1)
            quota_code = "L-34B43A08" if self.cfg.spot else "L-1216C47A"
            try:
                available_vcpus = self.quotas.get_service_quota(
                    ServiceCode="ec2", QuotaCode=quota_code
                )["Quota"]["Value"]
                quota = {
                    "required_vcpus": required_vcpus,
                    "available_vcpus": available_vcpus,
                    "quota_code": quota_code,
                }
                if available_vcpus < required_vcpus:
                    raise RuntimeError(
                        f"EC2 quota {quota_code} allows {available_vcpus:g} vCPUs; "
                        f"campaign needs {required_vcpus}")
            except ClientError as exc:
                code = exc.response.get("Error", {}).get("Code")
                if code not in {
                    "AccessDenied", "AccessDeniedException", "UnauthorizedOperation",
                }:
                    raise
                quota = {
                    "required_vcpus": required_vcpus,
                    "available_vcpus": None,
                    "quota_code": quota_code,
                }
        else:
            candidates = self.candidate_types()
            if not candidates:
                raise RuntimeError("no instance type satisfies the campaign minimums")
        return {
            "account": identity["Account"],
            "arn": identity["Arn"],
            "availability_zones": azs,
            "quota": quota,
        }

    def candidate_types(self) -> list[str]:
        """Return available types that meet the configured minimums, cheapest first."""
        want_nvme = self.cfg.min_local_nvme
        families = {f.lower() for f in self.cfg.instance_families}
        offered: set[str] = set()
        azs = [az for _s, _v, az in self._default_subnets()]
        for az in azs:
            pages = self.ec2.get_paginator("describe_instance_type_offerings").paginate(
                LocationType="availability-zone",
                Filters=[{"Name": "location", "Values": [az]}])
            for page in pages:
                offered.update(o["InstanceType"] for o in page["InstanceTypeOfferings"])
        if not offered:
            return []

        filters = [
            {"Name": "current-generation", "Values": ["true"]},
            {"Name": "supported-usage-class",
             "Values": ["spot" if self.cfg.spot else "on-demand"]},
            {"Name": "processor-info.supported-architecture", "Values": ["x86_64"]},
        ]
        if want_nvme:
            filters.append({"Name": "instance-storage-supported", "Values": ["true"]})
        viable: dict[str, tuple[int, int]] = {}
        pages = self.ec2.get_paginator("describe_instance_types").paginate(Filters=filters)
        for page in pages:
            for it in page["InstanceTypes"]:
                name = it["InstanceType"]
                if name not in offered:
                    continue
                letters = re.match(r"[a-z]+", name.split(".")[0])
                if not letters or letters.group(0) not in families:
                    continue
                # Exclude accelerators from CPU benchmark comparisons.
                if it.get("GpuInfo") or it.get("InferenceAcceleratorInfo"):
                    continue
                vcpu = it["VCpuInfo"]["DefaultVCpus"]
                mem_gib = it["MemoryInfo"]["SizeInMiB"] / 1024
                if vcpu < self.cfg.min_vcpu or mem_gib < self.cfg.min_mem_gib:
                    continue
                viable[name] = (vcpu, int(mem_gib))
        if not viable:
            return []

        price: dict[str, float] = {}
        try:
            hist = self.ec2.describe_spot_price_history(
                InstanceTypes=sorted(viable), ProductDescriptions=["Linux/UNIX"],
                MaxResults=len(viable) * 8)["SpotPriceHistory"]
            for h in hist:
                p = float(h["SpotPrice"])
                name = h["InstanceType"]
                if name not in price or p < price[name]:
                    price[name] = p
        except self.ec2.exceptions.ClientError:
            pass  # Fall back to size ordering.

        def key(name):
            vcpu, mem = viable[name]
            return (price.get(name, float("inf")), vcpu, mem, name)

        ordered = sorted(viable, key=key)
        return ordered[: self.cfg.max_type_candidates]

    def provision(self) -> list[Host]:
        """Provision validators and one control host in one AZ. Idempotent."""
        # A retained control host is excluded from this validator count.
        current = self.describe(states=("pending", "running"))
        want = self.cfg.nodes
        if current and len(current) != want:
            print(f"provision: replacing incomplete validator fleet "
                  f"({len(current)}/{want} instance(s))", flush=True)
            self.terminate(keep_control=True)
            self._wait_terminated()
        if self.cfg.instance_type:
            return self._provision_type()
        candidates = self.candidate_types()
        if not candidates:
            raise RuntimeError(
                f"no current-generation x86_64 type in {self.cfg.region} meets the "
                f"minimums (vcpu>={self.cfg.min_vcpu}, mem>={self.cfg.min_mem_gib} GiB, "
                f"local_nvme={self.cfg.min_local_nvme})")
        # Reject an existing multi-AZ fleet before handling capacity failures.
        self._fleet_az()
        print(f"provision: candidate types (cheapest first): {', '.join(candidates)}",
              flush=True)
        last = None
        for i, itype in enumerate(candidates):
            self.cfg.instance_type = itype
            print(f"provision: trying {itype} "
                  f"({i + 1}/{len(candidates)})", flush=True)
            try:
                return self._provision_type()
            except RuntimeError as e:
                last = e
                print(f"provision: {itype} could not supply the committee: {e}", flush=True)
                stale = self.describe(states=("pending", "running"))
                if stale:
                    print(f"provision: tearing down {len(stale)} partial {itype} "
                          f"instance(s) before the next candidate", flush=True)
                    self.terminate(keep_control=True)
                    self._wait_terminated()
        self.cfg.instance_type = ""
        raise RuntimeError(f"no candidate type could supply {self.cfg.nodes + 1} "
                           f"instances in {self.cfg.region}: {last}")

    def _provision_type(self) -> list[Host]:
        """Provision one fixed instance type."""
        target_az = self._fleet_az()
        want = self.cfg.nodes + 1
        if self.cfg.instance_type:
            live_validators = self._describe_instances(
                ("pending", "running"), role="validator")
            wrong = {i["InstanceType"] for i in live_validators
                     if i["InstanceType"] != self.cfg.instance_type}
            if wrong:
                # Validator instance types must be homogeneous.
                print(f"provision: live validators are {sorted(wrong)}, not "
                      f"{self.cfg.instance_type}; terminating and relaunching",
                      flush=True)
                self.terminate(keep_control=True)
                self._wait_terminated()
                target_az = self._fleet_az()
        current_all = self._describe_instances(("running",))
        if len(current_all) >= want:
            self._ensure_control_tag()
            hosts = self.describe()[: self.cfg.nodes]
            self._assert_single_az(hosts)
            return hosts
        subnets = self._default_subnets()
        gid = self.ensure_security_group(subnets[0][1])
        ami = self._ami()
        # Attach the size override to the AMI's root device.
        root_dev = self.ec2.describe_images(ImageIds=[ami])["Images"][0].get(
            "RootDeviceName", "/dev/sda1")

        def base_kwargs(subnet_id, count):
            return dict(
                ImageId=ami, InstanceType=self.cfg.instance_type,
                # Allow partial fills, but complete them only within this AZ.
                MinCount=1, MaxCount=count, KeyName=self.cfg.key_name,
                SubnetId=subnet_id, SecurityGroupIds=[gid],
                # The dead-man shutdown must release the instance and volume.
                InstanceInitiatedShutdownBehavior="terminate",
                UserData=base64.b64encode(_USER_DATA.encode()).decode(),
                TagSpecifications=[self._tag_spec("instance", role="validator")],
                BlockDeviceMappings=[{
                    "DeviceName": root_dev,
                    "Ebs": {"VolumeSize": self.cfg.disk_gb, "VolumeType": "gp3",
                            "DeleteOnTermination": True}}],
            )

        def msg_of(e):
            return str(e)

        def spot_opts():
            o = {"SpotInstanceType": "one-time", "InstanceInterruptionBehavior": "terminate"}
            if self.cfg.spot_max_price:
                o["MaxPrice"] = self.cfg.spot_max_price
            return {"MarketType": "spot", "SpotOptions": o}

        cap = ("InsufficientInstanceCapacity", "capacity-not-available",
               "MaxSpotInstanceCount", "SpotMaxPriceTooLow")
        # A fleet may accumulate partial fills only within one AZ.
        candidates = [s for s in subnets if target_az is None or s[2] == target_az]
        if not candidates:
            raise RuntimeError(f"existing fleet lives in {target_az} but that AZ has "
                               f"no default subnet")

        # Include locally returned IDs until EC2 describe calls become consistent.
        launched: set[str] = set()

        def have_count() -> int:
            described_ids = {i["InstanceId"]
                              for i in self._describe_instances(("pending", "running"))}
            dead_ids = {i["InstanceId"] for i in
                        self._describe_instances(("shutting-down", "terminated"))}
            launched.difference_update(dead_ids)
            return len(described_ids | launched)

        last = None
        for subnet_id, _vpc, az in candidates:
            while True:
                have = have_count()
                if have >= want:
                    self._wait_running()
                    self._ensure_control_tag()
                    hosts = self.describe()[: self.cfg.nodes]
                    self._assert_single_az(hosts)
                    return hosts
                got = 0
                for use_spot in ([True, False] if self.cfg.spot else [False]):
                    kwargs = base_kwargs(subnet_id, want - have)
                    if use_spot:
                        kwargs["InstanceMarketOptions"] = spot_opts()
                    try:
                        resp = self.ec2.run_instances(**kwargs)["Instances"]
                        launched.update(i["InstanceId"] for i in resp)
                        got = len(resp)
                        print(f"provision: {az} gave {got} "
                              f"{'spot' if use_spot else 'on-demand'} "
                              f"{self.cfg.instance_type} ({have + got}/{want})", flush=True)
                        break
                    except self.ec2.exceptions.ClientError as e:
                        if not any(s in msg_of(e) for s in cap):
                            raise
                        last = e
                        what = "Spot" if use_spot else "on-demand"
                        print(f"provision: no {what} {self.cfg.instance_type} capacity "
                              f"in {az}", flush=True)
                if got == 0:
                    break
            have = have_count()
            if have >= want:
                self._wait_running()
                self._ensure_control_tag()
                hosts = self.describe()[: self.cfg.nodes]
                self._assert_single_az(hosts)
                return hosts
            if target_az is not None:
                raise RuntimeError(
                    f"{az} cannot complete the committee ({have}/{want}) and the "
                    f"single-AZ rule forbids topping up in another AZ: {last}")
            if have:
                print(f"provision: {az} stalled at {have}/{want}; tearing its partial "
                      f"batch down before trying the next AZ", flush=True)
                self.terminate(keep_control=True)
                self._wait_terminated()
                launched.clear()
        raise RuntimeError(f"no single AZ in {self.cfg.region} could supply {want} x "
                           f"{self.cfg.instance_type}: {last}")

    def _fleet_az(self) -> str | None:
        """Return the pinned or existing fleet AZ; reject conflicts."""
        azs = {i["Placement"]["AvailabilityZone"]
               for i in self._describe_instances(("pending", "running"))}
        if len(azs) > 1:
            raise RuntimeError(f"fleet spans {sorted(azs)}; a run must live in ONE AZ "
                               f"-- terminate the minority instances first")
        live = azs.pop() if azs else None
        pinned = self.cfg.az
        if pinned and live and pinned != live:
            raise RuntimeError(
                f"config pins az={pinned} but this run already has live instances in "
                f"{live} (most likely a retained control host from an earlier, "
                f"unpinned run) -- terminate them first (`nuke`, or `down "
                f"--no-keep-monitoring`) so the whole fleet can be built in {pinned}")
        return pinned or live

    def _assert_single_az(self, hosts: list[Host]) -> None:
        """Verify that all live instances use the configured AZ."""
        seen = sorted({i["Placement"]["AvailabilityZone"]
                       for i in self._describe_instances(("pending", "running"))})
        if len(seen) > 1:
            raise RuntimeError(
                f"provisioned fleet spans {seen} -- the single-AZ rule was violated; "
                f"cross-AZ private-IP traffic is billed both ways and gives the "
                f"minority nodes a different latency baseline. Terminate and retry")
        where = seen[0] if seen else "?"
        if self.cfg.az and seen and seen[0] != self.cfg.az:
            raise RuntimeError(
                f"provisioned fleet is in {seen[0]} but config pins az={self.cfg.az}")
        print(f"provision: {len(hosts)} validator(s) + control in {where} "
              f"({'pinned' if self.cfg.az else 'auto-selected'}); intra-AZ private-IP "
              f"traffic is free", flush=True)

    def _wait_running(self) -> None:
        ids = [i["InstanceId"] for i in self._describe_instances(("pending", "running"))]
        if ids:
            self.ec2.get_waiter("instance_running").wait(InstanceIds=ids)

    def _describe_instances(self, states, role: str | None = None) -> list[dict]:
        """Return this run's matching instances ordered by instance ID."""
        filters = [
            {"Name": "tag:Project", "Values": ["wan-bench"]},
            {"Name": "tag:Run", "Values": [self.cfg.run_id]},
            {"Name": "instance-state-name", "Values": list(states)},
        ]
        if role is not None:
            filters.append({"Name": "tag:Role", "Values": [role]})
        resp = self.ec2.describe_instances(Filters=filters)
        insts = [i for r in resp["Reservations"] for i in r["Instances"]]
        insts.sort(key=lambda i: i["InstanceId"])
        return insts

    def _ensure_control_tag(self) -> None:
        """Assign the extra instance the control role once."""
        if self._describe_instances(("pending", "running"), role="control"):
            return
        live = self._describe_instances(("pending", "running"))
        if len(live) <= self.cfg.nodes:
            return
        control_id = live[self.cfg.nodes]["InstanceId"]
        self.ec2.create_tags(Resources=[control_id],
                              Tags=[{"Key": "Role", "Value": "control"}])

    def describe(self, states=("running",)) -> list[Host]:
        """Return validators in stable instance-ID order."""
        insts = self._describe_instances(states, role="validator")
        return [
            Host(index=idx, instance_id=i["InstanceId"],
                 public_ip=i.get("PublicIpAddress", ""),
                 private_ip=i["PrivateIpAddress"])
            for idx, i in enumerate(insts)
        ]

    def control_host(self) -> Host:
        """Return the run's control host."""
        insts = self._describe_instances(("running",), role="control")
        if not insts:
            raise RuntimeError("control instance not provisioned yet")
        i = insts[0]
        return Host(index=self.cfg.nodes, instance_id=i["InstanceId"],
                    public_ip=i.get("PublicIpAddress", ""),
                    private_ip=i["PrivateIpAddress"])

    def fleet_info(self) -> dict[str, str]:
        """Return effective validator placement, type, and image."""
        instances = self._describe_instances(("pending", "running"))
        if not instances:
            raise RuntimeError(f"run {self.cfg.run_id} has no live fleet")
        validators = self._describe_instances(
            ("pending", "running"), role="validator")
        if not validators:
            raise RuntimeError(f"run {self.cfg.run_id} has no live validators")

        def one(records, field, read):
            values = {read(instance) for instance in records}
            if len(values) != 1:
                raise RuntimeError(f"fleet has mixed {field}: {sorted(values)}")
            return values.pop()

        return {
            "az": one(instances, "availability zones",
                      lambda i: i["Placement"]["AvailabilityZone"]),
            "instance_type": one(validators, "validator instance types",
                                 lambda i: i["InstanceType"]),
            "ami": one(validators, "validator AMIs", lambda i: i["ImageId"]),
        }

    def terminate(self, keep_control: bool = False) -> list[str]:
        """Terminate this run's instances, optionally retaining the control host."""
        live = self._describe_instances(("pending", "running", "stopping", "stopped"))
        spare = None
        if keep_control:
            control = self._describe_instances(
                ("pending", "running", "stopping", "stopped"), role="control")
            if control:
                spare = control[0]["InstanceId"]
        ids = [i["InstanceId"] for i in live if i["InstanceId"] != spare]
        if ids:
            self.ec2.terminate_instances(InstanceIds=ids)
        return ids

    def _wait_terminated(self, role: str | None = "validator") -> None:
        """Poll and retry termination for this run and optional role."""
        deadline = time.monotonic() + 600
        while True:
            left = self._describe_instances(
                ("pending", "running", "shutting-down", "stopping", "stopped"),
                role=role)
            if not left:
                return
            if time.monotonic() > deadline:
                raise RuntimeError(
                    f"timed out waiting for {len(left)} instance(s) to terminate: "
                    f"{[i['InstanceId'] for i in left]}")
            self.ec2.terminate_instances(
                InstanceIds=[i["InstanceId"] for i in left])
            time.sleep(10)

    @staticmethod
    def nuke(region: str, profile: str | None = None) -> list[str]:
        """Terminate all wan-bench instances and delete their security groups."""
        ec2 = boto3.Session(profile_name=profile, region_name=region).client("ec2")
        filters = [
            {"Name": "tag:Project", "Values": ["wan-bench"]},
            {"Name": "instance-state-name",
             "Values": ["pending", "running", "stopping", "stopped"]},
        ]
        resp = ec2.describe_instances(Filters=filters)
        ids = [i["InstanceId"] for reservation in resp["Reservations"]
               for i in reservation["Instances"]]
        if ids:
            ec2.terminate_instances(InstanceIds=ids)
            deadline = time.monotonic() + 600
            while True:
                live = ec2.describe_instances(Filters=filters)
                remaining = [
                    i["InstanceId"] for reservation in live["Reservations"]
                    for i in reservation["Instances"]
                ]
                if not remaining:
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError(
                        f"timed out terminating wan-bench instances: {remaining}")
                ec2.terminate_instances(InstanceIds=remaining)
                time.sleep(10)

        groups = ec2.describe_security_groups(
            Filters=[{"Name": "tag:Project", "Values": ["wan-bench"]}]
        )["SecurityGroups"]
        for group in groups:
            deadline = time.monotonic() + 120
            while True:
                try:
                    ec2.delete_security_group(GroupId=group["GroupId"])
                    break
                except ClientError as exc:
                    code = exc.response.get("Error", {}).get("Code")
                    if code == "InvalidGroup.NotFound":
                        break
                    if code != "DependencyViolation" or time.monotonic() >= deadline:
                        raise
                    time.sleep(2)
        return ids
